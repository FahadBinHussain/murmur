package config

import (
	"os"
	"strconv"
	"strings"
)

type Platform string

const (
	PlatformMessenger     Platform = "messenger"
	PlatformFacebook      Platform = "facebook"
	PlatformMessengerLite Platform = "messenger-lite"
)

type Config struct {
	CookiesPath             string
	Platform                Platform
	LogLevel                string
	LiteLLMBase             string
	DefaultChat             string
	DefaultImage            string
	DatabaseURL             string
	ContextWindow           int
	WhatsAppEnabled         bool
	WhatsAppBinary          string
	WhatsAppStore           string
	WhatsAppAccount         string
	WhatsAppWebhookSecret   string
	WhatsAppMaxMessages     int
	WhatsAppDownloadMedia   bool
	WhatsAppProxy           string
	WACLI_SEND_WEBHOOK_URL  string
}

func Load() *Config {
	cfg := &Config{
		Platform: PlatformMessenger,
		LogLevel: "info",
	}

	if v := os.Getenv("MURMUR_COOKIES"); v != "" {
		cfg.CookiesPath = v
	}
	if v := os.Getenv("MURMUR_PLATFORM"); v != "" {
		cfg.Platform = Platform(strings.ToLower(v))
	}
	if v := os.Getenv("MURMUR_LOG_LEVEL"); v != "" {
		cfg.LogLevel = strings.ToLower(v)
	}

	if cfg.CookiesPath == "" {
		home, _ := os.UserHomeDir()
		cfg.CookiesPath = home + "/.config/murmur/cookies.json"
	}

	if v := os.Getenv("LITELLM_BASE"); v != "" {
		cfg.LiteLLMBase = v
	}
	if v := os.Getenv("DEFAULT_CHAT"); v != "" {
		cfg.DefaultChat = v
	}
	if v := os.Getenv("DEFAULT_IMAGE"); v != "" {
		cfg.DefaultImage = v
	}
	if v := os.Getenv("DATABASE_URL"); v != "" {
		cfg.DatabaseURL = v
	}
	if v := os.Getenv("CONTEXT_WINDOW"); v != "" {
		if n, err := strconv.Atoi(v); err == nil && n > 0 {
			cfg.ContextWindow = n
		}
	}
	if cfg.ContextWindow == 0 {
		cfg.ContextWindow = 100
	}

	if v := os.Getenv("WHATSAPP_ENABLED"); v == "1" || strings.ToLower(v) == "true" {
		cfg.WhatsAppEnabled = true
	}
	if v := os.Getenv("WHATSAPP_BINARY"); v != "" {
		cfg.WhatsAppBinary = v
	} else {
		cfg.WhatsAppBinary = "wacli"
	}
	if v := os.Getenv("WHATSAPP_STORE"); v != "" {
		cfg.WhatsAppStore = v
	}
	if v := os.Getenv("WHATSAPP_ACCOUNT"); v != "" {
		cfg.WhatsAppAccount = v
	}
	if v := os.Getenv("WHATSAPP_WEBHOOK_SECRET"); v != "" {
		cfg.WhatsAppWebhookSecret = v
	}
	if v := os.Getenv("WHATSAPP_MAX_MESSAGES"); v != "" {
		if n, err := strconv.Atoi(v); err == nil && n > 0 {
			cfg.WhatsAppMaxMessages = n
		}
	}
	if v := os.Getenv("WHATSAPP_DOWNLOAD_MEDIA"); v == "1" || strings.ToLower(v) == "true" {
		cfg.WhatsAppDownloadMedia = true
	}
	if v := os.Getenv("WHATSAPP_PROXY"); v != "" {
		cfg.WhatsAppProxy = v
	}
	if v := os.Getenv("WACLI_SEND_WEBHOOK_URL"); v != "" {
		cfg.WACLI_SEND_WEBHOOK_URL = v
	}

	return cfg
}

func (c *Config) Validate() error {
	if c.CookiesPath == "" {
		return ErrMissingCookiesPath
	}
	return nil
}

var ErrMissingCookiesPath = &ConfigError{"cookies_path is required"}

type ConfigError struct {
	Msg string
}

func (e *ConfigError) Error() string { return e.Msg }