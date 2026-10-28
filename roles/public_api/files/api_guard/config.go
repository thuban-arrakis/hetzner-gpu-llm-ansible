package main

import (
	"bytes"
	"encoding/json"
	"errors"
	"io"
	"net"
	"net/url"
	"os"
	"regexp"
)

type Config struct {
	Bind                  string  `json:"bind"`
	Upstream              string  `json:"upstream"`
	Username              string  `json:"username"`
	Model                 string  `json:"model"`
	ContextTokens         int     `json:"context_tokens"`
	OutputTokens          int     `json:"output_tokens"`
	BodyBytes             int64   `json:"body_bytes"`
	InputBytes            int     `json:"input_bytes"`
	MaxMessages           int     `json:"max_messages"`
	InferenceRPM          float64 `json:"inference_rpm"`
	InferenceBurst        int     `json:"inference_burst"`
	InferenceConcurrency  int     `json:"inference_concurrency"`
	RequestTimeoutSeconds int     `json:"request_timeout_seconds"`
	KnownPerIPRPM         float64 `json:"known_per_ip_rpm"`
	KnownPerIPBurst       int     `json:"known_per_ip_burst"`
	KnownGlobalRPM        float64 `json:"known_global_rpm"`
	KnownGlobalBurst      int     `json:"known_global_burst"`
	GrafanaPerIPRPM       float64 `json:"grafana_per_ip_rpm"`
	GrafanaPerIPBurst     int     `json:"grafana_per_ip_burst"`
	GrafanaGlobalRPM      float64 `json:"grafana_global_rpm"`
	GrafanaGlobalBurst    int     `json:"grafana_global_burst"`
	UnknownPerIPRPM       float64 `json:"unknown_per_ip_rpm"`
	UnknownPerIPBurst     int     `json:"unknown_per_ip_burst"`
	UnknownGlobalRPM      float64 `json:"unknown_global_rpm"`
	UnknownGlobalBurst    int     `json:"unknown_global_burst"`
	IPCapacity            int     `json:"ip_capacity"`
	IPTTLSeconds          int     `json:"ip_ttl_seconds"`
	CredentialTTLSeconds  int     `json:"credential_ttl_seconds"`
}

func defaultConfig() Config {
	return Config{Bind: "127.0.0.1:11435", Upstream: "http://127.0.0.1:11434", Username: "llm",
		Model: "qwen3:4b-q4_K_M", ContextTokens: 2048, OutputTokens: 256, BodyBytes: 32768, InputBytes: 8192, MaxMessages: 32,
		InferenceRPM: 6, InferenceBurst: 2, InferenceConcurrency: 1, RequestTimeoutSeconds: 120,
		KnownPerIPRPM: 300, KnownPerIPBurst: 80, KnownGlobalRPM: 600, KnownGlobalBurst: 120,
		GrafanaPerIPRPM: 1800, GrafanaPerIPBurst: 300, GrafanaGlobalRPM: 3600, GrafanaGlobalBurst: 600,
		UnknownPerIPRPM: 5, UnknownPerIPBurst: 2, UnknownGlobalRPM: 30, UnknownGlobalBurst: 4,
		IPCapacity: 4096, IPTTLSeconds: 600, CredentialTTLSeconds: 60}
}

func (c Config) validate() error {
	host, port, err := net.SplitHostPort(c.Bind)
	if err != nil || host != "127.0.0.1" || port == "0" {
		return errors.New("invalid loopback bind")
	}
	if _, err = net.LookupPort("tcp", port); err != nil {
		return errors.New("invalid bind port")
	}
	target, err := url.Parse(c.Upstream)
	if err != nil || target.Scheme != "http" || target.Hostname() != "127.0.0.1" || target.Port() == "" || target.User != nil || target.RawQuery != "" || target.Fragment != "" || (target.Path != "" && target.Path != "/") {
		return errors.New("invalid loopback upstream")
	}
	if _, err = net.LookupPort("tcp", target.Port()); err != nil {
		return errors.New("invalid upstream port")
	}
	if !regexp.MustCompile(`^[a-z][a-z0-9_-]{0,31}$`).MatchString(c.Username) || !regexp.MustCompile(`^[a-zA-Z0-9][a-zA-Z0-9._/+\-]{0,150}:[a-zA-Z0-9._+\-]{1,50}$`).MatchString(c.Model) {
		return errors.New("invalid configured identity")
	}
	if c.ContextTokens < 512 || c.ContextTokens > 32768 || c.OutputTokens < 1 || c.OutputTokens > 256 || c.BodyBytes < 1024 || c.BodyBytes > 32768 || c.InputBytes < 1 || c.InputBytes > 8192 || c.MaxMessages < 1 || c.MaxMessages > 32 || c.InferenceConcurrency != 1 || c.RequestTimeoutSeconds < 1 || c.RequestTimeoutSeconds > 120 {
		return errors.New("invalid request limits")
	}
	for _, v := range []float64{c.InferenceRPM, c.KnownPerIPRPM, c.KnownGlobalRPM, c.GrafanaPerIPRPM, c.GrafanaGlobalRPM, c.UnknownPerIPRPM, c.UnknownGlobalRPM} {
		if !(v > 0 && v <= 10000) {
			return errors.New("invalid rate")
		}
	}
	for _, v := range []int{c.InferenceBurst, c.KnownPerIPBurst, c.KnownGlobalBurst, c.GrafanaPerIPBurst, c.GrafanaGlobalBurst, c.UnknownPerIPBurst, c.UnknownGlobalBurst} {
		if v < 1 || v > 1000 {
			return errors.New("invalid burst")
		}
	}
	if c.IPCapacity < 1 || c.IPCapacity > 4096 || c.IPTTLSeconds < 1 || c.IPTTLSeconds > 600 || c.CredentialTTLSeconds < 1 || c.CredentialTTLSeconds > 60 {
		return errors.New("invalid cache limits")
	}
	return nil
}

func loadConfig(filename string) (Config, error) {
	c := defaultConfig()
	f, err := os.Open(filename)
	if err != nil {
		return c, errors.New("configuration unavailable")
	}
	defer f.Close()
	raw, err := io.ReadAll(io.LimitReader(f, 16385))
	if err != nil || len(raw) > 16384 {
		return c, errors.New("configuration too large")
	}
	value, err := parseObject(raw)
	if err != nil {
		return c, errors.New("invalid configuration JSON")
	}
	encoded, _ := json.Marshal(value)
	dec := json.NewDecoder(bytes.NewReader(encoded))
	dec.DisallowUnknownFields()
	if err = dec.Decode(&c); err != nil {
		return c, errors.New("invalid configuration fields")
	}
	return c, c.validate()
}
