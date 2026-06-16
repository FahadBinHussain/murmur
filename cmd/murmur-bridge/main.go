package main

import (
	"context"
	"encoding/json"
	"flag"
	"fmt"
	"io"
	"net/http"
	"os"
	"sync"

	"github.com/user/murmur/internal/bridge"
	"github.com/user/murmur/internal/config"
	"github.com/user/murmur/internal/cookies"
)

var (
	mu          sync.Mutex
	cookieState = "ok"
	cookieMsg   = "cookies loaded"
)

func main() {
	cookiesPath := flag.String("cookies", "", "Path to cookies JSON file (env: MURMUR_COOKIES)")
	platform := flag.String("platform", "", "Platform: messenger, facebook, messenger-lite (env: MURMUR_PLATFORM)")
	flag.Parse()

	cfg := config.Load()

	if *cookiesPath != "" {
		cfg.CookiesPath = *cookiesPath
	}
	if *platform != "" {
		cfg.Platform = config.Platform(*platform)
	}

	if err := cfg.Validate(); err != nil {
		panic(err)
	}

	// verify cookies load
	if _, err := cookies.LoadFromFile(cfg.CookiesPath); err != nil {
		cookieState = "error"
		cookieMsg = fmt.Sprintf("failed to load cookies: %v", err)
	}

	// API endpoints
	http.HandleFunc("/", func(w http.ResponseWriter, r *http.Request) {
		fmt.Fprintf(w, "OK")
	})

	http.HandleFunc("/api/cookies/status", func(w http.ResponseWriter, r *http.Request) {
		mu.Lock()
		defer mu.Unlock()
		w.Header().Set("Content-Type", "application/json")
		json.NewEncoder(w).Encode(map[string]string{
			"status":  cookieState,
			"message": cookieMsg,
			"path":    cfg.CookiesPath,
		})
	})

	http.HandleFunc("/api/cookies", func(w http.ResponseWriter, r *http.Request) {
		if r.Method != http.MethodPut {
			http.Error(w, "PUT required", http.StatusMethodNotAllowed)
			return
		}
		body, err := io.ReadAll(r.Body)
		if err != nil {
			http.Error(w, fmt.Sprintf("read error: %v", err), http.StatusBadRequest)
			return
		}
		if !json.Valid(body) {
			http.Error(w, "invalid JSON", http.StatusBadRequest)
			return
		}
		if err := os.WriteFile(cfg.CookiesPath, body, 0644); err != nil {
			http.Error(w, fmt.Sprintf("write error: %v", err), http.StatusInternalServerError)
			return
		}
		// verify it loads
		if _, err := cookies.LoadFromFile(cfg.CookiesPath); err != nil {
			mu.Lock()
			cookieState = "error"
			cookieMsg = fmt.Sprintf("saved but invalid: %v", err)
			mu.Unlock()
			http.Error(w, fmt.Sprintf("saved but invalid: %v", err), http.StatusBadRequest)
			return
		}
		mu.Lock()
		cookieState = "ok"
		cookieMsg = "cookies updated"
		mu.Unlock()
		w.Header().Set("Content-Type", "application/json")
		json.NewEncoder(w).Encode(map[string]string{
			"status":  "ok",
			"message": "cookies saved, restart space to apply",
		})
	})

	go http.ListenAndServe(":7860", nil)

	b := bridge.New(cfg)
	b.Run(context.Background(), os.Stdin, os.Stdout)
}