package main

import (
	"context"
	"flag"
	"os"

	"github.com/user/murmur/internal/bridge"
	"github.com/user/murmur/internal/config"
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

	b := bridge.New(cfg)
	b.Run(context.Background(), os.Stdin, os.Stdout)
}