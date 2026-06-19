package main

import (
	"context"
	"flag"
	"fmt"
	"os"

	"github.com/rs/zerolog/log"
	"github.com/user/murmur/internal/bnp"
	"github.com/user/murmur/internal/bridge"
	"github.com/user/murmur/internal/config"
	"github.com/user/murmur/internal/cookies"
	"github.com/user/murmur/internal/whatsapp"
)

var (
	theBridge *bridge.Bridge
	theCtx    context.Context
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
		log.Fatal().Err(err).Msg("failed to load cookies")
	}

	if cfg.DatabaseURL != "" {
		log.Info().Msg("DATABASE_URL: configured, connecting to database...")
	} else {
		log.Warn().Msg("DATABASE_URL: not set, running without persistence")
	}

	theCtx = context.Background()
	theBridge = bridge.New(theCtx, cfg)

	// Start BNP notification worker
	bnpWorker := bnp.NewWorker(theBridge, nil)
	go bnpWorker.Run(theCtx)

	// Start WhatsApp sync if enabled
	if cfg.WhatsAppEnabled {
		// Load session from Neon database before starting sync
		if err := theBridge.LoadSessionFromDB(theCtx); err != nil {
			log.Error().Err(err).Msg("Failed to load WhatsApp session from database")
		}

		webhookURL := fmt.Sprintf("http://localhost:7860/wacli/webhook")
		syncMgr := whatsapp.NewSyncManager(
			cfg.WhatsAppBinary,
			cfg.WhatsAppStore,
			cfg.WhatsAppAccount,
			webhookURL,
			cfg.WhatsAppWebhookSecret,
			cfg.WhatsAppMaxMessages,
			cfg.WhatsAppDownloadMedia,
			log.Logger.With().Str("component", "wacli-sync").Logger(),
		)
		theBridge.SetSyncManager(syncMgr)
		if err := syncMgr.Start(theCtx); err != nil {
			log.Error().Err(err).Msg("Failed to start WhatsApp sync")
		}
	}

	// Run bridge (blocks until context cancelled)
	theBridge.Run(theCtx, os.Stdin, os.Stdout)
}