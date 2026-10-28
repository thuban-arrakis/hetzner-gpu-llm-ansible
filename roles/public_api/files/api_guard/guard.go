package main

import (
	"bytes"
	"context"
	"errors"
	"fmt"
	"io"
	"mime"
	"net"
	"net/http"
	"net/url"
	"strconv"
	"strings"
	"sync/atomic"
	"time"
)

const responseLimit int64 = 4 * 1024 * 1024

type counters struct {
	admissions, preauthRejected, authRejected, inferenceRequests, inferenceLimited, payloadRejected, upstreamErrors, responseLimited, handlerLimited atomic.Uint64
	inflight                                                                                                                                         atomic.Int64
}
type Guard struct {
	cfg             Config
	admission       *admission
	now             func() time.Time
	client          *http.Client
	slots, handlers chan struct{}
	stats           counters
}

func newGuard(c Config) (*Guard, error) {
	if err := c.validate(); err != nil {
		return nil, err
	}
	transport := &http.Transport{Proxy: nil, DialContext: (&net.Dialer{Timeout: 5 * time.Second, KeepAlive: 30 * time.Second}).DialContext, MaxConnsPerHost: 2, MaxIdleConns: 2, MaxIdleConnsPerHost: 2, IdleConnTimeout: 30 * time.Second, DisableCompression: true, ResponseHeaderTimeout: time.Duration(c.RequestTimeoutSeconds) * time.Second}
	return &Guard{cfg: c, admission: newAdmission(c, time.Now()), now: time.Now, client: &http.Client{Transport: transport, CheckRedirect: func(*http.Request, []*http.Request) error { return http.ErrUseLastResponse }}, slots: make(chan struct{}, c.InferenceConcurrency), handlers: make(chan struct{}, 64)}, nil
}
func reject(w http.ResponseWriter, status int, code string, retry int) {
	w.Header().Set("Content-Type", "application/json")
	w.Header().Set("Cache-Control", "no-store")
	w.Header().Set("X-Content-Type-Options", "nosniff")
	if retry > 0 {
		w.Header().Set("Retry-After", strconv.Itoa(retry))
	}
	if status == http.StatusUnauthorized {
		w.Header().Set("WWW-Authenticate", `Basic realm="LLM API", charset="UTF-8"`)
	}
	w.WriteHeader(status)
	fmt.Fprintf(w, `{"error":{"message":"Request rejected by API limits","type":"api_guard","code":%q}}`+"\n", code)
}
func (g *Guard) ServeHTTP(w http.ResponseWriter, r *http.Request) {
	select {
	case g.handlers <- struct{}{}:
		defer func() { <-g.handlers }()
	default:
		g.stats.handlerLimited.Add(1)
		reject(w, 429, "handler_limit", 1)
		return
	}
	if r.URL.RawQuery != "" {
		reject(w, 400, "query_not_allowed", 0)
		return
	}
	switch r.URL.Path {
	case "/healthz":
		if r.Method != "GET" {
			reject(w, 405, "method_not_allowed", 0)
			return
		}
		w.WriteHeader(200)
	case "/metrics":
		if r.Method != "GET" {
			reject(w, 405, "method_not_allowed", 0)
			return
		}
		g.metrics(w)
	case "/admit":
		g.admit(w, r)
	case "/authenticated":
		g.authenticated(w, r)
	case "/api/chat", "/api/generate", "/v1/chat/completions", "/v1/completions":
		g.proxy(w, r)
	default:
		reject(w, 404, "not_found", 0)
	}
}
func (g *Guard) admit(w http.ResponseWriter, r *http.Request) {
	if r.Method != "GET" {
		reject(w, 405, "method_not_allowed", 0)
		return
	}
	g.stats.admissions.Add(1)
	ip, ok := clientIP(r)
	if !ok {
		g.stats.authRejected.Add(1)
		reject(w, 400, "invalid_client_address", 0)
		return
	}
	hash, shape := credentialHash(r, g.cfg.Username)
	if allowed, retry := g.admission.admit(ip, hash, shape, grafanaRequest(r), g.now()); !allowed {
		g.stats.preauthRejected.Add(1)
		reject(w, 429, "admission_limit", retry)
		return
	}
	if !shape {
		g.stats.authRejected.Add(1)
		reject(w, 401, "authentication_required", 0)
		return
	}
	w.WriteHeader(204)
}
func (g *Guard) authenticated(w http.ResponseWriter, r *http.Request) {
	if r.Method != "GET" {
		reject(w, 405, "method_not_allowed", 0)
		return
	}
	hash, shape := credentialHash(r, g.cfg.Username)
	if !shape || r.Header.Get("X-Guard-User") != g.cfg.Username {
		g.stats.authRejected.Add(1)
		reject(w, 401, "authentication_required", 0)
		return
	}
	g.admission.authenticate(hash, g.now())
	w.WriteHeader(204)
}
func (g *Guard) proxy(w http.ResponseWriter, r *http.Request) {
	if r.Method != "POST" {
		reject(w, 405, "method_not_allowed", 0)
		return
	}
	g.stats.inferenceRequests.Add(1)
	if r.Header.Get("X-Guard-User") != g.cfg.Username {
		g.stats.authRejected.Add(1)
		reject(w, 401, "authentication_required", 0)
		return
	}
	select {
	case g.slots <- struct{}{}:
		g.stats.inflight.Add(1)
		defer func() { g.stats.inflight.Add(-1); <-g.slots }()
	default:
		g.stats.inferenceLimited.Add(1)
		reject(w, 429, "inference_busy", 1)
		return
	}
	if allowed, retry := g.admission.inference(g.now()); !allowed {
		g.stats.inferenceLimited.Add(1)
		reject(w, 429, "inference_rate_limit", retry)
		return
	}
	ctx, cancel := context.WithTimeout(r.Context(), time.Duration(g.cfg.RequestTimeoutSeconds)*time.Second)
	defer cancel()
	controller := http.NewResponseController(w)
	deadline, _ := ctx.Deadline()
	_ = controller.SetReadDeadline(deadline)
	_ = controller.SetWriteDeadline(deadline)
	if encoding := r.Header.Get("Content-Encoding"); encoding != "" && encoding != "identity" {
		g.stats.payloadRejected.Add(1)
		reject(w, 415, "compressed_body_not_allowed", 0)
		return
	}
	media, parameters, err := mime.ParseMediaType(r.Header.Get("Content-Type"))
	if err != nil || media != "application/json" {
		g.stats.payloadRejected.Add(1)
		reject(w, 415, "json_required", 0)
		return
	}
	for key, value := range parameters {
		if key != "charset" || !strings.EqualFold(value, "utf-8") {
			g.stats.payloadRejected.Add(1)
			reject(w, 415, "json_required", 0)
			return
		}
	}
	raw, err := io.ReadAll(http.MaxBytesReader(w, r.Body, g.cfg.BodyBytes))
	if err != nil {
		g.stats.payloadRejected.Add(1)
		var tooLarge *http.MaxBytesError
		if errors.As(err, &tooLarge) {
			reject(w, 413, "body_limit", 0)
		} else {
			reject(w, 400, "body_unavailable", 0)
		}
		return
	}
	body, err := normalize(raw, r.URL.Path, g.cfg)
	if err != nil {
		g.stats.payloadRejected.Add(1)
		if errors.Is(err, errInputLimit) {
			reject(w, 413, "input_limit", 0)
		} else {
			reject(w, 400, "invalid_payload", 0)
		}
		return
	}
	target, err := url.Parse(g.cfg.Upstream)
	if err != nil {
		reject(w, 502, "upstream_unavailable", 0)
		return
	}
	target.Path = r.URL.Path
	upstream, err := http.NewRequestWithContext(ctx, "POST", target.String(), bytes.NewReader(body))
	if err != nil {
		reject(w, 502, "upstream_unavailable", 0)
		return
	}
	// New request, not a cloned one: no authorization, cookies, guard, forwarding
	// or arbitrary caller headers can reach the model server.
	upstream.Header.Set("Content-Type", "application/json")
	upstream.Header.Set("Accept", "application/json, text/event-stream")
	upstream.Header.Set("Accept-Encoding", "identity")
	response, err := g.client.Do(upstream)
	if err != nil {
		g.stats.upstreamErrors.Add(1)
		if ctx.Err() != nil {
			reject(w, 504, "upstream_timeout", 0)
		} else {
			reject(w, 502, "upstream_unavailable", 0)
		}
		return
	}
	defer response.Body.Close()
	if response.StatusCode >= 300 && response.StatusCode < 400 {
		g.stats.upstreamErrors.Add(1)
		reject(w, 502, "upstream_redirect_refused", 0)
		return
	}
	if response.ContentLength > responseLimit {
		g.stats.responseLimited.Add(1)
		reject(w, 502, "response_limit", 0)
		return
	}
	if encoding := response.Header.Get("Content-Encoding"); encoding != "" && encoding != "identity" {
		g.stats.upstreamErrors.Add(1)
		reject(w, 502, "upstream_encoding_refused", 0)
		return
	}
	contentType := response.Header.Get("Content-Type")
	if contentType == "" {
		contentType = "application/json"
	}
	w.Header().Set("Content-Type", contentType)
	w.Header().Set("Cache-Control", "no-store")
	w.Header().Set("X-Content-Type-Options", "nosniff")
	w.WriteHeader(response.StatusCode)
	buffer := make([]byte, 16*1024)
	var written int64
	for {
		count, readErr := response.Body.Read(buffer)
		if count > 0 {
			if written+int64(count) > responseLimit {
				g.stats.responseLimited.Add(1)
				cancel()
				panic(http.ErrAbortHandler)
			}
			if _, err = w.Write(buffer[:count]); err != nil {
				cancel()
				return
			}
			written += int64(count)
			if err = controller.Flush(); err != nil {
				cancel()
				return
			}
		}
		if readErr != nil {
			if readErr != io.EOF {
				g.stats.upstreamErrors.Add(1)
				cancel()
				panic(http.ErrAbortHandler)
			}
			return
		}
		if ctx.Err() != nil {
			cancel()
			panic(http.ErrAbortHandler)
		}
	}
}
func (g *Guard) metrics(w http.ResponseWriter) {
	w.Header().Set("Content-Type", "text/plain; version=0.0.4")
	values := []struct {
		name  string
		value uint64
	}{
		{"admissions_total", g.stats.admissions.Load()}, {"preauth_rejections_total", g.stats.preauthRejected.Load()},
		{"authentication_rejections_total", g.stats.authRejected.Load()}, {"inference_requests_total", g.stats.inferenceRequests.Load()},
		{"inference_rejections_total", g.stats.inferenceLimited.Load()}, {"payload_rejections_total", g.stats.payloadRejected.Load()},
		{"upstream_errors_total", g.stats.upstreamErrors.Load()}, {"response_limit_rejections_total", g.stats.responseLimited.Load()},
		{"handler_limit_rejections_total", g.stats.handlerLimited.Load()},
	}
	for _, item := range values {
		fmt.Fprintf(w, "# TYPE llm_guard_%s counter\nllm_guard_%s %d\n", item.name, item.name, item.value)
	}
	fmt.Fprintf(w, "# TYPE llm_guard_inflight gauge\nllm_guard_inflight %d\n", g.stats.inflight.Load())
}
