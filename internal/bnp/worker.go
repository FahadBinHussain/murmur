package bnp

import (
	"bytes"
	"context"
	"encoding/json"
	"fmt"
	"net/http"
	"os"
	"strconv"
	"time"

	"github.com/user/murmur/internal/bridge"
)

type Settings struct {
	OutboxURL   string
	Token       string
	ThreadID    string
	PollSeconds int
	ClaimLimit  int
	Timeout     int
}

func LoadSettings() *Settings {
	s := &Settings{
		OutboxURL:   os.Getenv("BNP_MESSENGER_OUTBOX_URL"),
		Token:       os.Getenv("BNP_MESSENGER_OUTBOX_TOKEN"),
		ThreadID:    os.Getenv("BNP_MESSENGER_THREAD_ID"),
		PollSeconds: 30,
		ClaimLimit:  2,
		Timeout:     30,
	}
	if v := os.Getenv("BNP_MESSENGER_POLL_SECONDS"); v != "" {
		if n, err := strconv.Atoi(v); err == nil && n >= 5 && n <= 3600 {
			s.PollSeconds = n
		}
	}
	if v := os.Getenv("BNP_MESSENGER_CLAIM_LIMIT"); v != "" {
		if n, err := strconv.Atoi(v); err == nil && n >= 1 && n <= 10 {
			s.ClaimLimit = n
		}
	}
	if v := os.Getenv("BNP_MESSENGER_REQUEST_TIMEOUT_SECONDS"); v != "" {
		if n, err := strconv.Atoi(v); err == nil && n >= 5 && n <= 120 {
			s.Timeout = n
		}
	}
	return s
}

func (s *Settings) Enabled() bool {
	return s.OutboxURL != "" && s.Token != "" && s.ThreadID != ""
}

type Item struct {
	ID        string `json:"id"`
	Message   string `json:"message"`
	Phase     string `json:"phase"`
	Status    string `json:"status"`
	MessageID string `json:"messageId"`
}

type Worker struct {
	client     *bridge.Bridge
	settings   *Settings
	httpClient *http.Client
}

func NewWorker(b *bridge.Bridge, s *Settings) *Worker {
	if s == nil {
		s = LoadSettings()
	}
	return &Worker{
		client:     b,
		settings:   s,
		httpClient: &http.Client{Timeout: time.Duration(s.Timeout) * time.Second},
	}
}

func (w *Worker) Run(ctx context.Context) {
	if !w.settings.Enabled() {
		return
	}
	fmt.Printf("BNP Messenger notifications enabled for thread %s\n", w.settings.ThreadID)
	ticker := time.NewTicker(time.Duration(w.settings.PollSeconds) * time.Second)
	defer ticker.Stop()
	for {
		select {
		case <-ctx.Done():
			return
		case <-ticker.C:
			if err := w.processOnce(ctx); err != nil {
				fmt.Printf("BNP Messenger notification worker failed: %v\n", err)
			}
		}
	}
}

func (w *Worker) processOnce(ctx context.Context) error {
	items, err := w.claimItems(ctx)
	if err != nil {
		return err
	}
	for _, item := range items {
		w.sendItem(ctx, item)
	}
	return nil
}

func (w *Worker) claimItems(ctx context.Context) ([]Item, error) {
	req, err := http.NewRequestWithContext(ctx, "GET", w.settings.OutboxURL, nil)
	if err != nil {
		return nil, err
	}
	q := req.URL.Query()
	q.Set("limit", strconv.Itoa(w.settings.ClaimLimit))
	req.URL.RawQuery = q.Encode()
	req.Header.Set("Authorization", "Bearer "+w.settings.Token)

	resp, err := w.httpClient.Do(req)
	if err != nil {
		return nil, err
	}
	defer resp.Body.Close()

	var body struct {
		Items []Item `json:"items"`
	}
	if err := json.NewDecoder(resp.Body).Decode(&body); err != nil {
		return nil, fmt.Errorf("decode: %w", err)
	}
	if resp.StatusCode >= 400 {
		return nil, fmt.Errorf("HTTP %d: %+v", resp.StatusCode, body)
	}
	return body.Items, nil
}

func (w *Worker) sendItem(ctx context.Context, item Item) {
	if item.ID == "" || item.Message == "" {
		return
	}
	threadID, err := strconv.ParseInt(w.settings.ThreadID, 10, 64)
	if err != nil {
		fmt.Printf("BNP invalid thread ID: %v\n", err)
		w.ackItem(ctx, item.ID, "failed", "", "", err.Error())
		return
	}

	if item.Status == "edit_pending" && item.MessageID != "" {
		if err := w.client.EditMessage(ctx, threadID, item.MessageID, item.Message); err != nil {
			w.ackItem(ctx, item.ID, "failed", "", "", err.Error())
			return
		}
		w.ackItem(ctx, item.ID, "sent", item.MessageID, "edit", "")
		return
	}

	msgID := w.client.SendMessage(ctx, threadID, item.Message)
	if msgID == "" {
		w.ackItem(ctx, item.ID, "failed", "", "", "send returned empty message ID")
		return
	}
	w.ackItem(ctx, item.ID, "sent", msgID, "send", "")
}

func (w *Worker) ackItem(ctx context.Context, itemID, status, messageID, mode, errMsg string) {
	payload := map[string]string{"id": itemID, "status": status}
	if mode != "" {
		payload["mode"] = mode
	}
	if messageID != "" {
		payload["messageId"] = messageID
	}
	if errMsg != "" {
		if len(errMsg) > 2000 {
			errMsg = errMsg[:2000]
		}
		payload["error"] = errMsg
	}
	body, _ := json.Marshal(payload)
	req, err := http.NewRequestWithContext(ctx, "POST", w.settings.OutboxURL, bytes.NewReader(body))
	if err != nil {
		fmt.Printf("BNP ack create request failed: %v\n", err)
		return
	}
	req.Header.Set("Authorization", "Bearer "+w.settings.Token)
	req.Header.Set("Content-Type", "application/json")
	resp, err := w.httpClient.Do(req)
	if err != nil {
		fmt.Printf("BNP ack request failed: %v\n", err)
		return
	}
	resp.Body.Close()
	if resp.StatusCode >= 400 {
		fmt.Printf("BNP ack failed with HTTP %d\n", resp.StatusCode)
	}
}
