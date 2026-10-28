package main

import (
	"context"
	"flag"
	"fmt"
	"io"
	"log"
	"net/http"
	"os"
	"os/signal"
	"syscall"
	"time"
)

func main() {
	filename := flag.String("config", "/etc/llm-api-guard/config.json", "Root-managed non-secret configuration reference")
	flag.Parse()
	cfg, err := loadConfig(*filename)
	if err != nil {
		fmt.Fprintln(os.Stderr, "api_guard_configuration_failed")
		os.Exit(1)
	}
	guard, err := newGuard(cfg)
	if err != nil {
		fmt.Fprintln(os.Stderr, "api_guard_configuration_failed")
		os.Exit(1)
	}
	server := &http.Server{Addr: cfg.Bind, Handler: guard, ReadHeaderTimeout: 5 * time.Second, ReadTimeout: 15 * time.Second, WriteTimeout: time.Duration(cfg.RequestTimeoutSeconds+5) * time.Second, IdleTimeout: 30 * time.Second, MaxHeaderBytes: 8192, ErrorLog: log.New(io.Discard, "", 0)}
	stopped := make(chan os.Signal, 1)
	signal.Notify(stopped, syscall.SIGTERM, syscall.SIGINT)
	shutdownDone := make(chan struct{})
	go func() {
		defer close(shutdownDone)
		<-stopped
		ctx, cancel := context.WithTimeout(context.Background(), 10*time.Second)
		defer cancel()
		if server.Shutdown(ctx) != nil {
			_ = server.Close()
		}
		guard.client.CloseIdleConnections()
	}()
	if err = server.ListenAndServe(); err != nil && err != http.ErrServerClosed {
		fmt.Fprintln(os.Stderr, "api_guard_listener_failed")
		os.Exit(1)
	}
	<-shutdownDone
}
