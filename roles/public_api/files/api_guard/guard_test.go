package main

import (
	"bytes"
	"context"
	"encoding/json"
	"fmt"
	"io"
	"net/http"
	"net/http/httptest"
	"strings"
	"sync/atomic"
	"testing"
	"time"
)

func testGuard(t *testing.T, upstream http.HandlerFunc) *Guard {
	t.Helper()
	server := httptest.NewServer(upstream)
	t.Cleanup(server.Close)
	c := defaultConfig()
	c.Upstream = server.URL
	c.InferenceRPM = 1000
	c.InferenceBurst = 100
	g, err := newGuard(c)
	if err != nil {
		t.Fatal("guard configuration failed")
	}
	t.Cleanup(g.client.CloseIdleConnections)
	return g
}
func body(route string) string {
	if route == "/api/generate" || route == "/v1/completions" {
		return `{"model":"qwen3:4b-q4_K_M","prompt":"hello","stream":false}`
	}
	return `{"model":"qwen3:4b-q4_K_M","messages":[{"role":"user","content":"hello"}],"stream":false}`
}
func inferenceRequest(route, payload string) *http.Request {
	r := httptest.NewRequest("POST", route, strings.NewReader(payload))
	r.Header.Set("Content-Type", "application/json")
	r.Header.Set("X-Guard-User", "llm")
	return r
}

func TestNativeProxyRebuildsBoundedBodyAndStripsHeaders(t *testing.T) {
	var received map[string]any
	var headers http.Header
	g := testGuard(t, func(w http.ResponseWriter, r *http.Request) {
		headers = r.Header.Clone()
		if err := json.NewDecoder(r.Body).Decode(&received); err != nil {
			t.Error("invalid upstream JSON")
		}
		w.Header().Set("Content-Type", "application/json")
		_, _ = io.WriteString(w, `{"done":true,"response":"ok"}`)
	})
	r := inferenceRequest("/api/chat", `{"model":"qwen3:4b-q4_K_M","messages":[{"role":"user","content":"hello"}],"options":{"num_ctx":512,"num_predict":8,"temperature":0.2},"stream":false}`)
	r.Header.Set("Authorization", "not-forwarded")
	r.Header.Set("Cookie", "not-forwarded")
	r.Header.Set("X-Guard-Client-IP", "192.0.2.1")
	r.Header.Set("X-Forwarded-For", "192.0.2.2")
	r.Header.Set("X-Untrusted", "not-forwarded")
	w := httptest.NewRecorder()
	g.ServeHTTP(w, r)
	if w.Code != 200 {
		t.Fatalf("unexpected status %d", w.Code)
	}
	options := received["options"].(map[string]any)
	if options["num_ctx"] != float64(2048) || options["num_predict"] != float64(8) || received["think"] != false || received["keep_alive"] != "5m" {
		t.Fatal("resource policy not normalized")
	}
	for _, key := range []string{"Authorization", "Cookie", "X-Guard-Client-IP", "X-Guard-User", "X-Forwarded-For", "X-Untrusted"} {
		if headers.Get(key) != "" {
			t.Fatalf("header forwarded: %s", key)
		}
	}
}

func TestOpenAIAliasAndDefaults(t *testing.T) {
	raw := []byte(`{"model":"qwen3:4b-q4_K_M","messages":[{"role":"user","content":"hello"}],"max_completion_tokens":8}`)
	normalized, err := normalize(raw, "/v1/chat/completions", defaultConfig())
	if err != nil {
		t.Fatal("normalization failed")
	}
	result, _ := parseObject(normalized)
	if result["max_tokens"] != json.Number("8") || result["stream"] != false || result["n"] != json.Number("1") || result["reasoning_effort"] != "none" {
		t.Fatal("OpenAI defaults or alias not enforced")
	}
	if _, exists := result["max_completion_tokens"]; exists {
		t.Fatal("unsupported alias forwarded")
	}
}

func TestStrictPayloadPolicy(t *testing.T) {
	cases := []struct{ route, payload string }{
		{"/api/generate", `{"model":"qwen3:4b-q4_K_M","prompt":"x","prompt":"y"}`},
		{"/api/generate", `{"model":"qwen3:4b-q4_K_M","prompt":"x","options":{"num_predict":1,"num_predict":2}}`},
		{"/api/generate", body("/api/generate") + ` {}`},
		{"/api/generate", `{"model":"other:latest","prompt":"x"}`},
		{"/api/generate", `{"model":"qwen3:4b-q4_K_M","Prompt":"x"}`},
		{"/api/generate", `{"model":"qwen3:4b-q4_K_M","prompt":"x","raw":true}`},
		{"/api/generate", `{"model":"qwen3:4b-q4_K_M","prompt":"x","keep_alive":-1}`},
		{"/api/generate", `{"model":"qwen3:4b-q4_K_M","prompt":"x","context":[1]}`},
		{"/api/generate", `{"model":"qwen3:4b-q4_K_M","prompt":"x","think":true}`},
		{"/api/generate", `{"model":"qwen3:4b-q4_K_M","prompt":"x","options":{"num_ctx":32768}}`},
		{"/api/generate", `{"model":"qwen3:4b-q4_K_M","prompt":"x","options":{"num_predict":-1}}`},
		{"/api/generate", `{"model":"qwen3:4b-q4_K_M","prompt":"x","options":{"num_predict":257}}`},
		{"/api/generate", `{"model":"qwen3:4b-q4_K_M","prompt":"x","options":{"num_thread":128}}`},
		{"/api/chat", `{"model":"qwen3:4b-q4_K_M","messages":[{"role":"user","content":"x","images":["x"]}]}`},
		{"/api/chat", `{"model":"qwen3:4b-q4_K_M","messages":[{"role":"tool","content":"x"}]}`},
		{"/v1/completions", `{"model":"qwen3:4b-q4_K_M","prompt":["x","y"]}`},
		{"/v1/completions", `{"model":"qwen3:4b-q4_K_M","prompt":"x","n":2}`},
		{"/v1/completions", `{"model":"qwen3:4b-q4_K_M","prompt":"x","max_tokens":1,"max_completion_tokens":1}`},
		{"/v1/completions", `{"model":"qwen3:4b-q4_K_M","prompt":"x","options":{"num_ctx":1}}`},
	}
	for i, item := range cases {
		t.Run(fmt.Sprint(i), func(t *testing.T) {
			if _, err := normalize([]byte(item.payload), item.route, defaultConfig()); err == nil {
				t.Fatal("unsafe payload accepted")
			}
		})
	}
}

func TestBodyInputAndMessageLimits(t *testing.T) {
	g := testGuard(t, func(w http.ResponseWriter, r *http.Request) { t.Error("rejected request reached upstream") })
	cases := []struct {
		payload, encoding string
		status            int
	}{
		{strings.Repeat(" ", 32769), "", 413},
		{fmt.Sprintf(`{"model":"qwen3:4b-q4_K_M","prompt":%q}`, strings.Repeat("x", 8193)), "", 413},
		{body("/api/generate"), "gzip", 415},
	}
	for _, item := range cases {
		w := httptest.NewRecorder()
		r := inferenceRequest("/api/generate", item.payload)
		r.Header.Set("Content-Encoding", item.encoding)
		g.ServeHTTP(w, r)
		if w.Code != item.status {
			t.Fatalf("expected %d got %d", item.status, w.Code)
		}
	}
	messages := strings.TrimSuffix(strings.Repeat(`{"role":"user","content":"x"},`, 33), ",")
	if _, err := normalize([]byte(`{"model":"qwen3:4b-q4_K_M","messages":[`+messages+`]}`), "/api/chat", defaultConfig()); err == nil {
		t.Fatal("message limit not enforced")
	}
}

func authRequest(route, ip string) *http.Request {
	r := httptest.NewRequest("GET", route, nil)
	r.Header.Set("X-Guard-Client-IP", ip)
	r.SetBasicAuth("llm", strings.Repeat("a", 43))
	return r
}
func status(g *Guard, r *http.Request) int {
	w := httptest.NewRecorder()
	g.ServeHTTP(w, r)
	return w.Code
}

func TestPreauthenticationBudgetsAndVerifiedCredentialCache(t *testing.T) {
	g, _ := newGuard(defaultConfig())
	now := time.Now()
	g.now = func() time.Time { return now }
	for i := 0; i < 2; i++ {
		if status(g, authRequest("/admit", "192.0.2.1")) != 204 {
			t.Fatal("initial unknown burst refused")
		}
	}
	if status(g, authRequest("/admit", "192.0.2.1")) != 429 {
		t.Fatal("unknown burst unbounded")
	}
	wrong := authRequest("/authenticated", "192.0.2.1")
	wrong.Header.Set("X-Guard-User", "other")
	if status(g, wrong) != 401 {
		t.Fatal("spoofed identity accepted")
	}
	verified := authRequest("/authenticated", "192.0.2.1")
	verified.Header.Set("X-Guard-User", "llm")
	if status(g, verified) != 204 {
		t.Fatal("verified credential not recorded")
	}
	for i := 0; i < 10; i++ {
		if status(g, authRequest("/admit", "192.0.2.1")) != 204 {
			t.Fatal("verified asset burst refused")
		}
	}
	now = now.Add(61 * time.Second)
	for i := 0; i < 2; i++ {
		if status(g, authRequest("/admit", "192.0.2.1")) != 204 {
			t.Fatal("expired cache initial budget refused")
		}
	}
	if status(g, authRequest("/admit", "192.0.2.1")) != 429 {
		t.Fatal("credential cache did not expire")
	}
}

func TestMissingCredentialsAndSpoofedForwardedAddress(t *testing.T) {
	g, _ := newGuard(defaultConfig())
	r := httptest.NewRequest("GET", "/admit", nil)
	r.Header.Set("X-Guard-Client-IP", "192.0.2.1")
	r.Header.Set("X-Forwarded-For", "192.0.2.99")
	for i := 0; i < 2; i++ {
		if status(g, r) != 401 {
			t.Fatal("missing credentials not challenged")
		}
	}
	r.Header.Set("X-Forwarded-For", "192.0.2.88")
	if status(g, r) != 429 {
		t.Fatal("forwarded address bypassed socket-IP budget")
	}
	proxy := inferenceRequest("/api/generate", body("/api/generate"))
	proxy.Header.Del("X-Guard-User")
	if status(g, proxy) != 401 {
		t.Fatal("unverified inference accepted")
	}
}

func TestKnownRequestsStillHaveGlobalAndPerIPLimits(t *testing.T) {
	c := defaultConfig()
	c.KnownPerIPBurst = 2
	c.KnownGlobalBurst = 3
	g, _ := newGuard(c)
	verified := authRequest("/authenticated", "192.0.2.1")
	verified.Header.Set("X-Guard-User", "llm")
	status(g, verified)
	if status(g, authRequest("/admit", "192.0.2.1")) != 204 || status(g, authRequest("/admit", "192.0.2.1")) != 204 || status(g, authRequest("/admit", "192.0.2.1")) != 429 {
		t.Fatal("known per-IP budget not enforced")
	}
	if status(g, authRequest("/admit", "192.0.2.2")) != 204 || status(g, authRequest("/admit", "192.0.2.3")) != 429 {
		t.Fatal("known global budget not enforced")
	}
}

func TestIPMapCapacityFailsClosedAndExpires(t *testing.T) {
	c := defaultConfig()
	c.IPCapacity = 1
	c.IPTTLSeconds = 1
	g, _ := newGuard(c)
	now := time.Now()
	g.now = func() time.Time { return now }
	if status(g, authRequest("/admit", "192.0.2.1")) != 204 || status(g, authRequest("/admit", "192.0.2.2")) != 429 {
		t.Fatal("IP capacity bypass")
	}
	now = now.Add(2 * time.Second)
	if status(g, authRequest("/admit", "192.0.2.2")) != 204 {
		t.Fatal("expired IP not reclaimed")
	}
}

func TestInferenceRateLimitHasRetryAfter(t *testing.T) {
	g := testGuard(t, func(w http.ResponseWriter, r *http.Request) { _, _ = io.WriteString(w, "ok") })
	g.admission.userInference = newBucket(6, 2, g.now())
	g.admission.globalInference = newBucket(6, 2, g.now())
	for i := 0; i < 2; i++ {
		if status(g, inferenceRequest("/api/generate", body("/api/generate"))) != 200 {
			t.Fatal("initial inference refused")
		}
	}
	w := httptest.NewRecorder()
	g.ServeHTTP(w, inferenceRequest("/api/generate", body("/api/generate")))
	if w.Code != 429 || w.Header().Get("Retry-After") == "" {
		t.Fatal("inference rate not bounded")
	}
}

func TestBusyRequestRejectedAndCancellationReleasesSlot(t *testing.T) {
	started, canceled := make(chan struct{}, 1), make(chan struct{}, 1)
	var calls atomic.Int32
	g := testGuard(t, func(w http.ResponseWriter, r *http.Request) {
		_, _ = io.Copy(io.Discard, r.Body)
		if calls.Add(1) == 1 {
			started <- struct{}{}
			<-r.Context().Done()
			canceled <- struct{}{}
			return
		}
		_, _ = io.WriteString(w, "ok")
	})
	server := httptest.NewServer(g)
	defer server.Close()
	ctx, cancel := context.WithCancel(context.Background())
	defer cancel()
	request, _ := http.NewRequestWithContext(ctx, "POST", server.URL+"/api/generate", strings.NewReader(body("/api/generate")))
	request.Header.Set("Content-Type", "application/json")
	request.Header.Set("X-Guard-User", "llm")
	finished := make(chan struct{})
	go func() {
		defer close(finished)
		response, _ := http.DefaultClient.Do(request)
		if response != nil {
			response.Body.Close()
		}
	}()
	select {
	case <-started:
	case <-time.After(time.Second):
		t.Fatal("upstream did not start")
	}
	if status(g, inferenceRequest("/api/generate", body("/api/generate"))) != 429 {
		t.Fatal("busy request queued")
	}
	cancel()
	select {
	case <-canceled:
	case <-time.After(time.Second):
		t.Fatal("disconnect did not cancel upstream")
	}
	<-finished
	until := time.Now().Add(time.Second)
	for g.stats.inflight.Load() != 0 && time.Now().Before(until) {
		time.Sleep(time.Millisecond)
	}
	if status(g, inferenceRequest("/api/generate", body("/api/generate"))) != 200 {
		t.Fatal("slot not released")
	}
}

func TestAbsoluteDeadlineStopsContinuallyStreamingUpstream(t *testing.T) {
	done := make(chan struct{}, 1)
	g := testGuard(t, func(w http.ResponseWriter, r *http.Request) {
		ticker := time.NewTicker(30 * time.Millisecond)
		defer ticker.Stop()
		for {
			select {
			case <-r.Context().Done():
				done <- struct{}{}
				return
			case <-ticker.C:
				_, _ = io.WriteString(w, "data: x\n\n")
				w.(http.Flusher).Flush()
			}
		}
	})
	g.cfg.RequestTimeoutSeconds = 1
	server := httptest.NewServer(g)
	defer server.Close()
	request, _ := http.NewRequest("POST", server.URL+"/api/generate", strings.NewReader(body("/api/generate")))
	request.Header.Set("Content-Type", "application/json")
	request.Header.Set("X-Guard-User", "llm")
	started := time.Now()
	response, err := http.DefaultClient.Do(request)
	if err == nil {
		_, _ = io.Copy(io.Discard, response.Body)
		response.Body.Close()
	}
	if time.Since(started) > 2*time.Second {
		t.Fatal("stream extended absolute deadline")
	}
	select {
	case <-done:
	case <-time.After(time.Second):
		t.Fatal("deadline did not cancel upstream")
	}
}

func TestResponseCapAndRedirectRefusal(t *testing.T) {
	for _, redirect := range []bool{false, true} {
		t.Run(fmt.Sprint(redirect), func(t *testing.T) {
			g := testGuard(t, func(w http.ResponseWriter, r *http.Request) {
				if redirect {
					w.Header().Set("Location", "http://127.0.0.1:1/")
					w.WriteHeader(302)
					return
				}
				w.Header().Set("Content-Length", fmt.Sprint(responseLimit+1))
				w.WriteHeader(200)
			})
			if status(g, inferenceRequest("/api/generate", body("/api/generate"))) != 502 {
				t.Fatal("unsafe upstream response accepted")
			}
		})
	}
}

func TestMetricsContainNoClientOrCredentialLabels(t *testing.T) {
	g, _ := newGuard(defaultConfig())
	status(g, authRequest("/admit", "192.0.2.1"))
	w := httptest.NewRecorder()
	g.ServeHTTP(w, httptest.NewRequest("GET", "/metrics", nil))
	if bytes.Contains(w.Body.Bytes(), []byte("192.0.2.1")) || bytes.Contains(w.Body.Bytes(), []byte("{")) {
		t.Fatal("metric labels expose request identity")
	}
	if !strings.Contains(w.Body.String(), "llm_guard_inflight 0") {
		t.Fatal("missing bounded metrics")
	}
}

func TestUnknownGlobalLimitAndCredentialCacheAreBounded(t *testing.T) {
	g, _ := newGuard(defaultConfig())
	for i := 0; i < 4; i++ {
		if status(g, authRequest("/admit", fmt.Sprintf("192.0.2.%d", i+1))) != 204 {
			t.Fatal("unknown initial global burst refused")
		}
	}
	if status(g, authRequest("/admit", "192.0.2.99")) != 429 {
		t.Fatal("unknown global budget not enforced")
	}
	for i := 0; i < 20; i++ {
		r := authRequest("/authenticated", "192.0.2.1")
		r.SetBasicAuth("llm", strings.Repeat(string(rune('a'+i)), 43))
		r.Header.Set("X-Guard-User", "llm")
		if status(g, r) != 204 {
			t.Fatal("verified credential rejected")
		}
	}
	if len(g.admission.known) != 8 {
		t.Fatal("credential cache is unbounded")
	}
}

func TestChunkedResponseLimitReleasesSlot(t *testing.T) {
	g := testGuard(t, func(w http.ResponseWriter, r *http.Request) {
		_, _ = io.Copy(io.Discard, r.Body)
		w.WriteHeader(200)
		w.(http.Flusher).Flush()
		chunk := make([]byte, 32*1024)
		for written := int64(0); written <= responseLimit; written += int64(len(chunk)) {
			if _, err := w.Write(chunk); err != nil {
				return
			}
			w.(http.Flusher).Flush()
		}
	})
	server := httptest.NewServer(g)
	defer server.Close()
	r, _ := http.NewRequest("POST", server.URL+"/api/generate", strings.NewReader(body("/api/generate")))
	r.Header.Set("X-Guard-User", "llm")
	r.Header.Set("Content-Type", "application/json")
	response, err := http.DefaultClient.Do(r)
	if err != nil {
		t.Fatal("stream did not start")
	}
	count, _ := io.Copy(io.Discard, response.Body)
	response.Body.Close()
	if count > responseLimit {
		t.Fatal("response limit exceeded")
	}
	until := time.Now().Add(time.Second)
	for g.stats.inflight.Load() != 0 && time.Now().Before(until) {
		time.Sleep(time.Millisecond)
	}
	if g.stats.responseLimited.Load() != 1 || g.stats.inflight.Load() != 0 {
		t.Fatal("response cap did not abort and release")
	}
}

func TestTotalHandlerLimitFailsClosed(t *testing.T) {
	g, _ := newGuard(defaultConfig())
	for i := 0; i < 64; i++ {
		g.handlers <- struct{}{}
	}
	if status(g, httptest.NewRequest("GET", "/healthz", nil)) != 429 {
		t.Fatal("handler cap bypassed")
	}
	for i := 0; i < 64; i++ {
		<-g.handlers
	}
	if status(g, httptest.NewRequest("GET", "/healthz", nil)) != 200 {
		t.Fatal("handler cap did not recover")
	}
}
