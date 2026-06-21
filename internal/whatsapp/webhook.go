package whatsapp

import (
	"context"
	"crypto/hmac"
	"crypto/sha256"
	"encoding/hex"
	"encoding/json"
	"fmt"
	"io"
	"net/http"
	"strings"
	"time"

	"github.com/rs/zerolog"
)

type ParsedMessage struct {
	Chat             ChatJID   `json:"Chat"`
	ID               string    `json:"ID"`
	SenderJID        string    `json:"SenderJID"`
	Timestamp        time.Time `json:"Timestamp"`
	FromMe           bool      `json:"FromMe"`
	Text             string    `json:"Text"`
	PushName         string    `json:"PushName"`
	ReplyToID        string    `json:"ReplyToID"`
	ReplyToSenderJID string    `json:"ReplyToSenderJID"`
	ReplyToDisplay   string    `json:"ReplyToDisplay"`
	ReactionToID     string    `json:"ReactionToID"`
	ReactionEmoji    string    `json:"ReactionEmoji"`
	IsForwarded      bool      `json:"IsForwarded"`
	Edited           bool      `json:"Edited"`
	Revoked          bool      `json:"Revoked"`
	Media            *Media    `json:"Media"`
	Poll             *Poll     `json:"Poll"`
}

type ChatJID struct {
	User   string `json:"user"`
	Server string `json:"server"`
}

func (c ChatJID) String() string {
	if c.Server != "" && c.Server != "s.whatsapp.net" {
		return c.User + "@" + c.Server
	}
	return c.User + "@s.whatsapp.net"
}

type Media struct {
	Type     string `json:"Type"`
	Caption  string `json:"Caption"`
	Filename string `json:"Filename"`
	MimeType string `json:"MimeType"`
}

type Poll struct {
	Question        string   `json:"Question"`
	Options         []string `json:"Options"`
	SelectableCount uint32   `json:"SelectableCount"`
}

type MessageHandler func(ctx context.Context, msg ParsedMessage)

type WebhookServer struct {
	secret   string
	handler  MessageHandler
	logger   zerolog.Logger
	server   *http.Server
}

func NewWebhookServer(secret string, handler MessageHandler, logger zerolog.Logger) *WebhookServer {
	return &WebhookServer{
		secret:  secret,
		handler: handler,
		logger:  logger,
	}
}

func (ws *WebhookServer) Start(addr string) error {
	mux := http.NewServeMux()
	mux.HandleFunc("/wacli/webhook", ws.handleWebhook)
	mux.HandleFunc("/api/health", func(w http.ResponseWriter, r *http.Request) {
		w.WriteHeader(http.StatusOK)
		w.Write([]byte("ok"))
	})

	ws.server = &http.Server{
		Addr:    addr,
		Handler: mux,
	}

	ws.logger.Info().Str("addr", addr).Msg("Starting WhatsApp webhook server")
	if err := ws.server.ListenAndServe(); err != nil && err != http.ErrServerClosed {
		return fmt.Errorf("webhook server: %w", err)
	}
	return nil
}

func (ws *WebhookServer) Shutdown() error {
	if ws.server != nil {
		return ws.server.Shutdown(nil)
	}
	return nil
}

func (ws *WebhookServer) handleWebhook(w http.ResponseWriter, r *http.Request) {
	if r.Method != http.MethodPost {
		http.Error(w, "Method not allowed", http.StatusMethodNotAllowed)
		return
	}

	body, err := io.ReadAll(r.Body)
	if err != nil {
		http.Error(w, "Failed to read body", http.StatusBadRequest)
		return
	}

	if ws.secret != "" {
		sig := r.Header.Get("X-Wacli-Signature")
		if !ws.verifySignature(body, sig) {
			ws.logger.Warn().Str("sig", sig).Msg("Invalid webhook signature")
			http.Error(w, "Invalid signature", http.StatusUnauthorized)
			return
		}
	}

	var msg ParsedMessage
	if err := json.Unmarshal(body, &msg); err != nil {
		ws.logger.Error().Err(err).Msg("Failed to parse webhook payload")
		http.Error(w, "Invalid JSON", http.StatusBadRequest)
		return
	}

	ws.logger.Info().
		Str("chat", msg.Chat.String()).
		Str("id", msg.ID).
		Str("sender", msg.SenderJID).
		Str("text", truncate(msg.Text, 100)).
		Bool("from_me", msg.FromMe).
		Msg("WhatsApp message received")

	ws.handler(r.Context(), msg)

	w.WriteHeader(http.StatusOK)
	w.Write([]byte(`{"status":"ok"}`))
}

func (ws *WebhookServer) verifySignature(payload []byte, sig string) bool {
	if !strings.HasPrefix(sig, "sha256=") {
		return false
	}
	sigHex := strings.TrimPrefix(sig, "sha256=")
	expected := computeHMAC(payload, ws.secret)
	return hmac.Equal([]byte(sigHex), []byte(expected))
}

func computeHMAC(payload []byte, secret string) string {
	mac := hmac.New(sha256.New, []byte(secret))
	mac.Write(payload)
	return hex.EncodeToString(mac.Sum(nil))
}

func truncate(s string, max int) string {
	if len(s) <= max {
		return s
	}
	return s[:max] + "..."
}

func VerifySignature(payload []byte, sig, secret string) bool {
	if !strings.HasPrefix(sig, "sha256=") {
		return false
	}
	sigHex := strings.TrimPrefix(sig, "sha256=")
	expected := computeHMAC(payload, secret)
	return hmac.Equal([]byte(sigHex), []byte(expected))
}
