package main

import (
	"crypto/sha256"
	"math"
	"net/http"
	"net/netip"
	"regexp"
	"strings"
	"sync"
	"time"
)

type bucket struct {
	tokens, capacity, rate float64
	updated                time.Time
}

func newBucket(rpm float64, burst int, now time.Time) *bucket {
	return &bucket{float64(burst), float64(burst), rpm / 60, now}
}
func (b *bucket) refill(now time.Time) {
	if now.After(b.updated) {
		b.tokens = math.Min(b.capacity, b.tokens+now.Sub(b.updated).Seconds()*b.rate)
		b.updated = now
	}
}
func takeBuckets(now time.Time, buckets ...*bucket) (bool, int) {
	retry := 0
	for _, b := range buckets {
		b.refill(now)
		if b.tokens < 1 {
			retry = max(retry, int(math.Ceil((1-b.tokens)/b.rate)))
		}
	}
	if retry > 0 {
		return false, retry
	}
	for _, b := range buckets {
		b.tokens--
	}
	return true, 0
}

type ipState struct {
	api, grafana, unknown *bucket
	seen                  time.Time
}
type admission struct {
	mu                                                                      sync.Mutex
	cfg                                                                     Config
	ips, anonymous                                                          map[netip.Addr]*ipState
	known                                                                   map[[32]byte]time.Time
	apiGlobal, grafanaGlobal, unknownGlobal, userInference, globalInference *bucket
	lastSweep                                                               time.Time
}

func newAdmission(c Config, now time.Time) *admission {
	return &admission{cfg: c, ips: make(map[netip.Addr]*ipState), anonymous: make(map[netip.Addr]*ipState), known: make(map[[32]byte]time.Time), apiGlobal: newBucket(c.KnownGlobalRPM, c.KnownGlobalBurst, now), grafanaGlobal: newBucket(c.GrafanaGlobalRPM, c.GrafanaGlobalBurst, now), unknownGlobal: newBucket(c.UnknownGlobalRPM, c.UnknownGlobalBurst, now), userInference: newBucket(c.InferenceRPM, c.InferenceBurst, now), globalInference: newBucket(c.InferenceRPM, c.InferenceBurst, now), lastSweep: now}
}

func grafanaRequest(r *http.Request) bool {
	// Caddy overwrites this on its loopback-only admission subrequest. Do not
	// classify arbitrary prefixes such as /grafana-other as dashboard traffic.
	path := r.Header.Get("X-Guard-Path")
	return path == "/grafana" || strings.HasPrefix(path, "/grafana/")
}

var passwordShape = regexp.MustCompile(`^[A-Za-z0-9_-]{32,128}$`)

func credentialHash(r *http.Request, username string) ([32]byte, bool) {
	if len(r.Header.Get("Authorization")) > 256 {
		return [32]byte{}, false
	}
	user, password, ok := r.BasicAuth()
	if !ok || user != username || !passwordShape.MatchString(password) {
		return [32]byte{}, false
	}
	// Canonical decoded credentials defeat alternate Base64 spelling/cache churn.
	return sha256.Sum256([]byte(user + "\x00" + password)), true
}
func clientIP(r *http.Request) (netip.Addr, bool) {
	ip, err := netip.ParseAddr(r.Header.Get("X-Guard-Client-IP"))
	return ip.Unmap(), err == nil
}
func (a *admission) sweep(now time.Time) {
	for hash, until := range a.known {
		if !now.Before(until) {
			delete(a.known, hash)
		}
	}
	if now.Sub(a.lastSweep) >= 30*time.Second || len(a.ips) >= a.cfg.IPCapacity {
		for _, states := range []map[netip.Addr]*ipState{a.ips, a.anonymous} {
			for ip, state := range states {
				if now.Sub(state.seen) >= time.Duration(a.cfg.IPTTLSeconds)*time.Second {
					delete(states, ip)
				}
			}
		}
		a.lastSweep = now
	}
}
func (a *admission) admit(ip netip.Addr, hash [32]byte, shape, grafana bool, now time.Time) (bool, int) {
	a.mu.Lock()
	defer a.mu.Unlock()
	a.sweep(now)
	_, known := a.known[hash]
	known = known && shape
	states := a.ips
	if !shape {
		// Cheap 401 challenges must not occupy the entries that preserve
		// plausible-password limits or exclude a new authenticated client.
		states = a.anonymous
	}
	state, exists := states[ip]
	var evict netip.Addr
	if !exists {
		if shape && len(states) >= a.cfg.IPCapacity {
			return false, 1
		}
		if !shape && len(states) >= min(1024, a.cfg.IPCapacity) {
			var oldest time.Time
			for address, entry := range states {
				if oldest.IsZero() || entry.seen.Before(oldest) {
					oldest, evict = entry.seen, address
				}
			}
		}
		state = &ipState{api: newBucket(a.cfg.KnownPerIPRPM, a.cfg.KnownPerIPBurst, now), grafana: newBucket(a.cfg.GrafanaPerIPRPM, a.cfg.GrafanaPerIPBurst, now), unknown: newBucket(a.cfg.UnknownPerIPRPM, a.cfg.UnknownPerIPBurst, now), seen: now}
	}
	buckets := []*bucket{a.apiGlobal, state.api}
	if grafana {
		buckets = []*bucket{a.grafanaGlobal, state.grafana}
	}
	// Missing or malformed credentials never reach bcrypt, so challenging them
	// must not spend the scarce allowance for plausible password attempts.
	if shape && !known {
		buckets = append(buckets, a.unknownGlobal, state.unknown)
	}
	ok, retry := takeBuckets(now, buckets...)
	if exists {
		state.seen = now
	}
	if ok && !exists {
		if evict.IsValid() {
			delete(states, evict)
		}
		states[ip] = state
	}
	return ok, retry
}
func (a *admission) authenticate(hash [32]byte, now time.Time) {
	a.mu.Lock()
	defer a.mu.Unlock()
	a.sweep(now)
	if _, exists := a.known[hash]; !exists && len(a.known) >= 8 {
		var oldestHash [32]byte
		var oldest time.Time
		for h, until := range a.known {
			if oldest.IsZero() || until.Before(oldest) {
				oldestHash = h
				oldest = until
			}
		}
		delete(a.known, oldestHash)
	}
	a.known[hash] = now.Add(time.Duration(a.cfg.CredentialTTLSeconds) * time.Second)
}
func (a *admission) inference(now time.Time) (bool, int) {
	a.mu.Lock()
	defer a.mu.Unlock()
	return takeBuckets(now, a.userInference, a.globalInference)
}
