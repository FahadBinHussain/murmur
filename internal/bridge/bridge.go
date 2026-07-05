package bridge

import (
	"bufio"
	"bytes"
	"context"
	"encoding/json"
	"errors"
	"fmt"
	"io"
	"net/http"
	"os"
	"os/signal"
	"strconv"
	"strings"
	"sync"
	"syscall"
	"time"

	"github.com/rs/zerolog"
	"github.com/rs/zerolog/log"

	"github.com/user/murmur/internal/ai"
	"github.com/user/murmur/internal/config"
	"github.com/user/murmur/internal/cookies"
	"github.com/user/murmur/internal/database"
	"github.com/user/murmur/internal/whatsapp"

	"go.mau.fi/mautrix-meta/pkg/messagix"
	"go.mau.fi/mautrix-meta/pkg/messagix/socket"
	"go.mau.fi/mautrix-meta/pkg/messagix/table"
	"go.mau.fi/mautrix-meta/pkg/messagix/types"
)

type OutgoingEvent struct {
	Type string      `json:"type"`
	Data interface{} `json:"data,omitempty"`
}

type IncomingCommand struct {
	Type string          `json:"type"`
	Data json.RawMessage `json:"data,omitempty"`
	ID   string          `json:"id,omitempty"`
}

type SendMessageData struct {
	ThreadID int64  `json:"thread_id"`
	Text     string `json:"text"`
}

type EditMessageData struct {
	MessageID string `json:"message_id"`
	Text      string `json:"text"`
}

type DeleteMessageData struct {
	MessageID string `json:"message_id"`
}

type MessageEventData struct {
	MessageID string `json:"message_id"`
	ThreadID  int64  `json:"thread_id"`
	SenderID  int64  `json:"sender_id"`
	Text      string `json:"text"`
	Timestamp int64  `json:"timestamp"`
	IsUnsent  bool   `json:"is_unsent,omitempty"`
}

type ReadyData struct {
	UID  int64  `json:"uid"`
	Name string `json:"name"`
}

type Bridge struct {
	cfg              *config.Config
	client           *messagix.Client
	ai               *ai.Client
	db               *database.DB
	logger           zerolog.Logger
	cancel           context.CancelFunc
	uid              int64
	threadModels     map[int64]string
	threadImgModels  map[int64]string
	pendingModels    map[int64][]string
	pendingImgModels map[int64][]string
	httpServer       *http.Server
	waSender         *whatsapp.Sender
	waSync           *whatsapp.SyncManager
	waClient         *whatsapp.WhatsmeowClient
	threadChannel    map[int64]string
	waJIDs           map[int64]string
	stdout           io.Writer
	startTime        time.Time
	outbox           []map[string]string
	outboxMu         sync.Mutex
}

func writeEvent(w io.Writer, evt OutgoingEvent) {
	data, err := json.Marshal(evt)
	if err != nil {
		log.Error().Err(err).Msg("Failed to marshal event")
		return
	}
	fmt.Fprintln(w, string(data))
}

func New(ctx context.Context, cfg *config.Config) *Bridge {
	zerolog.TimeFieldFormat = zerolog.TimeFormatUnixMs
	log.Logger = log.Output(zerolog.NewConsoleWriter(func(w *zerolog.ConsoleWriter) {
		w.NoColor = true
		w.Out = os.Stderr
	}))

	var plat types.Platform
	switch cfg.Platform {
	case config.PlatformFacebook:
		plat = types.Facebook
	case config.PlatformMessengerLite:
		plat = types.MessengerLite
	default:
		plat = types.Messenger
	}

	cookieMap, err := cookies.LoadFromFile(cfg.CookiesPath)
	if err != nil {
		log.Fatal().Err(err).Msg("Failed to load cookies")
	}

	c := cookies.ToMessagix(cookieMap, plat)
	if missing := cookies.GetMissing(c); len(missing) > 0 {
		log.Fatal().Strs("missing", missing).Msg("Missing required cookies")
	}

	logger := log.Logger.With().Str("component", "messagix").Logger()
	client := messagix.NewClient(c, logger, &messagix.Config{
		MayConnectToDGW: false,
	})

	b := &Bridge{
		cfg:              cfg,
		client:           client,
		ai:               ai.NewClient(cfg.LiteLLMBase, cfg.DefaultChat, cfg.DefaultImage),
		logger:           logger,
		threadModels:     make(map[int64]string),
		threadImgModels:  make(map[int64]string),
		pendingModels:    make(map[int64][]string),
		pendingImgModels: make(map[int64][]string),
		threadChannel:    make(map[int64]string),
		waJIDs:           make(map[int64]string),
	}

	if cfg.DatabaseURL != "" {
		db, err := database.New(ctx, cfg.DatabaseURL)
		if err != nil {
			log.Error().Err(err).Msg("Failed to connect to database, running without persistence")
		} else {
			b.db = db
			b.loadSavedModels(ctx)
		}
	}

	if cfg.WhatsAppEnabled {
		waLogger := log.Logger.With().Str("component", "whatsapp").Logger()
		storeDir := cfg.WhatsAppStore
		if storeDir == "" {
			home, _ := os.UserHomeDir()
			storeDir = home + "/.murmur-whatsapp"
		}
		// Try to create store dir, fall back to /app/whatsapp if read-only
		testFile := storeDir + "/.write-test"
		if err := os.MkdirAll(storeDir, 0700); err != nil || os.WriteFile(testFile, []byte("test"), 0600) != nil {
			storeDir = "/app/whatsapp"
			os.MkdirAll(storeDir, 0700)
			os.Remove(testFile)
		} else {
			os.Remove(testFile)
		}
		dbPath := storeDir + "/whatsmeow.db"
		log.Info().Str("dbPath", dbPath).Msg("Creating whatsmeow client")
		b.waClient, err = whatsapp.NewWhatsmeowClient(dbPath, cfg.WhatsAppProxy, waLogger, b.HandleWhatsAppMessage)
		if err != nil {
			log.Error().Err(err).Msg("Failed to create whatsmeow client")
		} else {
			log.Info().Str("db", dbPath).Msg("WhatsApp integration enabled (whatsmeow)")
		}
	}

	return b
}

func (b *Bridge) SetSyncManager(sm *whatsapp.SyncManager) {
	b.waSync = sm
}

func (b *Bridge) SetWhatsmeowClient(client *whatsapp.WhatsmeowClient) {
	b.waClient = client
}

func (b *Bridge) WAClient() *whatsapp.WhatsmeowClient {
	return b.waClient
}

func (b *Bridge) LoadSessionFromDB(ctx context.Context) error {
	if b.db == nil {
		return nil
	}
	session, err := b.db.LoadWhatsAppSession(ctx)
	if err != nil {
		b.logger.Warn().Err(err).Msg("No WhatsApp session in database")
		return nil
	}

	storeDir := b.cfg.WhatsAppStore
	if storeDir == "" {
		home, _ := os.UserHomeDir()
		storeDir = home + "/.wacli"
	}
	if err := os.MkdirAll(storeDir, 0700); err != nil {
		b.logger.Warn().Err(err).Str("dir", storeDir).Msg("Cannot write to configured store, falling back to /app/wacli")
		storeDir = "/app/wacli"
		if err := os.MkdirAll(storeDir, 0700); err != nil {
			return fmt.Errorf("create fallback store dir: %w", err)
		}
		b.cfg.WhatsAppStore = storeDir
	}

	if err := os.WriteFile(storeDir+"/session.db", session.SessionData, 0600); err != nil {
		b.logger.Warn().Err(err).Str("dir", storeDir).Msg("Cannot write to store, falling back to /app/wacli")
		storeDir = "/app/wacli"
		os.MkdirAll(storeDir, 0700)
		b.cfg.WhatsAppStore = storeDir
		if err := os.WriteFile(storeDir+"/session.db", session.SessionData, 0600); err != nil {
			return fmt.Errorf("write session.db: %w", err)
		}
	}
	b.logger.Info().Int("bytes", len(session.SessionData)).Str("updated", session.UpdatedAt).Msg("Loaded session.db from database")

	if len(session.WacliData) > 0 {
		if err := os.WriteFile(storeDir+"/wacli.db", session.WacliData, 0600); err != nil {
			b.logger.Warn().Err(err).Msg("Failed to write wacli.db")
		} else {
			b.logger.Info().Int("bytes", len(session.WacliData)).Msg("Loaded wacli.db from database")
		}
	}

	b.logger.Info().Str("store", b.cfg.WhatsAppStore).Msg("WhatsApp session loaded from database")
	return nil
}

func (b *Bridge) loadSavedModels(ctx context.Context) {
	if b.db == nil {
		return
	}
	models, err := b.db.GetAllThreadModels(ctx)
	if err != nil {
		log.Error().Err(err).Msg("Failed to load saved models")
		return
	}
	for threadID, m := range models {
		if m.ChatModel != "" {
			b.threadModels[threadID] = m.ChatModel
		}
		if m.ImageModel != "" {
			b.threadImgModels[threadID] = m.ImageModel
		}
	}
	log.Info().Int("threads", len(models)).Msg("Loaded saved model selections")
}

func (b *Bridge) ReloadCookies(ctx context.Context) error {
	log.Info().Msg("ReloadCookies: starting")
	cookieMap, err := cookies.LoadFromFile(b.cfg.CookiesPath)
	if err != nil {
		log.Error().Err(err).Msg("ReloadCookies: failed to load cookies")
		return fmt.Errorf("load cookies: %w", err)
	}
	log.Info().Int("count", len(cookieMap)).Msg("ReloadCookies: cookies loaded")

	var plat types.Platform
	switch b.cfg.Platform {
	case config.PlatformFacebook:
		plat = types.Facebook
	case config.PlatformMessengerLite:
		plat = types.MessengerLite
	default:
		plat = types.Messenger
	}

	c := cookies.ToMessagix(cookieMap, plat)
	if missing := cookies.GetMissing(c); len(missing) > 0 {
		log.Error().Strs("missing", missing).Msg("ReloadCookies: missing required cookies")
		return fmt.Errorf("missing cookies: %v", missing)
	}
	log.Info().Msg("ReloadCookies: all required cookies present")

	log.Info().Msg("ReloadCookies: disconnecting old client")
	b.client.Disconnect()

	logger := log.Logger.With().Str("component", "messagix").Logger()
	b.client = messagix.NewClient(c, logger, &messagix.Config{
		MayConnectToDGW: false,
	})
	log.Info().Msg("ReloadCookies: new messagix client created")

	// Re-attach event handler so incoming messages are processed after reconnect
	// Use context.Background() because the HTTP request context dies when the POST response is sent.
	if b.stdout != nil {
		b.client.SetEventHandler(b.makeEventHandler(context.Background(), b.stdout))
		log.Info().Msg("ReloadCookies: event handler re-attached")
	} else {
		log.Warn().Msg("ReloadCookies: b.stdout is nil, cannot re-attach event handler")
	}

	// Load messages page to initialize broker (required before Connect)
	log.Info().Msg("ReloadCookies: loading messages page to init broker")
	userInfo, _, err := b.client.LoadMessagesPage(context.Background())
	if err != nil {
		log.Error().Err(err).Msg("ReloadCookies: LoadMessagesPage failed")
		return fmt.Errorf("load messages page: %w", err)
	}
	b.uid = userInfo.GetFBID()
	log.Info().Int64("uid", b.uid).Str("name", userInfo.GetName()).Msg("ReloadCookies: logged in")

	log.Info().Msg("ReloadCookies: launching connect goroutine")
	go func() {
		log.Info().Msg("ReloadCookies: connect goroutine started")
		err := b.client.Connect(context.Background())
		if err != nil {
			log.Error().Err(err).Msg("ReloadCookies: reconnect failed")
		} else {
			log.Info().Msg("ReloadCookies: reconnect succeeded")
		}
	}()

	log.Info().Msg("ReloadCookies: done")
	return nil
}

func (b *Bridge) makeEventHandler(ctx context.Context, stdout io.Writer) func(context.Context, any) {
	return func(evtCtx context.Context, evt any) {
		log.Info().Str("type", fmt.Sprintf("%T", evt)).Msg("event_handler: received event")
		switch e := evt.(type) {
		case *messagix.Event_Ready:
			log.Info().Any("connection_code", e.ConnectionCode).Msg("MQTT connected")
			writeEvent(stdout, OutgoingEvent{Type: "mqtt_connected"})
			go func() {
				ticker := time.NewTicker(60 * time.Second)
				defer ticker.Stop()
				for {
					select {
					case <-ctx.Done():
						return
					case <-ticker.C:
						_, err := b.client.ExecuteTasks(ctx, &socket.ReportAppStateTask{
							AppState:  table.FOREGROUND,
							RequestID: fmt.Sprintf("keepalive-%d", time.Now().UnixMilli()),
						})
						if err != nil {
							log.Warn().Err(err).Msg("Foreground keepalive failed")
						}
					}
				}
			}()

		case *messagix.Event_PublishResponse:
			log.Info().Any("topic", e.Topic).Int64("request_id", e.Data.RequestID).
				Bool("table_nil", e.Table == nil).
				Msg("PublishResponse received")
			if e.Table == nil {
				log.Info().Msg("PublishResponse: table is nil, returning")
				return
			}
			upsertMessages, insertMessages := e.Table.WrapMessages()
			log.Info().Int("insert_count", len(insertMessages)).Int("upsert_count", len(upsertMessages)).Msg("PublishResponse: wrapping messages")

			for _, msg := range insertMessages {
				if msg == nil || msg.LSInsertMessage == nil {
					continue
				}
				if msg.IsUnsent {
					continue
				}
				evt := MessageEventData{
					MessageID: msg.MessageId,
					ThreadID:  msg.ThreadKey,
					SenderID:  msg.SenderId,
					Text:      msg.Text,
					Timestamp: msg.TimestampMs,
					IsUnsent:  msg.IsUnsent,
				}
				log.Info().Str("msg_id", msg.MessageId).Int64("thread_id", msg.ThreadKey).Int64("sender_id", msg.SenderId).Str("text", msg.Text).Msg("event_handler: insert message")
				writeEvent(stdout, OutgoingEvent{Type: "message", Data: evt})

				if msg.SenderId != b.uid {
					ts := time.UnixMilli(msg.TimestampMs)
					if ts.Before(b.startTime.Add(-5 * time.Second)) {
						continue
					}
					should := b.shouldRespond(msg.LSInsertMessage)
					log.Info().Bool("should_respond", should).Int64("sender_id", msg.SenderId).Int64("uid", b.uid).Msg("event_handler: checking shouldRespond")
					if should {
						text := msg.Text
						log.Info().Str("text", text).Msg("event_handler: calling handleAICommand")
						if strings.HasPrefix(strings.TrimSpace(text), "/ai") {
							b.handleAICommand(ctx, msg.ThreadKey, text)
						} else {
							cleaned := b.cleanPrompt(text, msg.LSInsertMessage)
							if cleaned != "" {
								b.handleAICommand(ctx, msg.ThreadKey, "/ai "+cleaned)
							}
						}
					}
				}
				_ = upsertMessages
			}

			for threadID, upsert := range upsertMessages {
				for _, msg := range upsert.Messages {
					if msg == nil || msg.LSInsertMessage == nil {
						continue
					}
					if msg.IsUnsent {
						continue
					}
					evt := MessageEventData{
						MessageID: msg.MessageId,
						ThreadID:  threadID,
						SenderID:  msg.SenderId,
						Text:      msg.Text,
						Timestamp: msg.TimestampMs,
						IsUnsent:  msg.IsUnsent,
					}
					log.Info().Str("msg_id", msg.MessageId).Int64("thread_id", threadID).Int64("sender_id", msg.SenderId).Str("text", msg.Text).Msg("event_handler: upsert message")
					writeEvent(stdout, OutgoingEvent{Type: "message", Data: evt})

					if msg.SenderId != b.uid {
						ts := time.UnixMilli(msg.TimestampMs)
						if ts.Before(b.startTime.Add(-5 * time.Second)) {
							continue
						}
						should := b.shouldRespond(msg.LSInsertMessage)
						log.Info().Bool("should_respond", should).Int64("sender_id", msg.SenderId).Int64("uid", b.uid).Msg("event_handler: checking shouldRespond (upsert)")
						if should {
							text := msg.Text
							log.Info().Str("text", text).Msg("event_handler: calling handleAICommand (upsert)")
							if strings.HasPrefix(strings.TrimSpace(text), "/ai") {
								b.handleAICommand(ctx, threadID, text)
							} else {
								cleaned := b.cleanPrompt(text, msg.LSInsertMessage)
								if cleaned != "" {
									b.handleAICommand(ctx, threadID, "/ai "+cleaned)
								}
							}
						}
					}
				}
			}

		case *messagix.Event_SocketError:
			log.Error().Err(e.Err).Int("attempts", e.ConnectionAttempts).Msg("Socket error")
			writeEvent(stdout, OutgoingEvent{
				Type: "socket_error",
				Data: map[string]interface{}{
					"error":    e.Err.Error(),
					"attempts": e.ConnectionAttempts,
				},
			})

		case *messagix.Event_PermanentError:
			log.Error().Err(e.Err).Msg("Permanent error")
			writeEvent(stdout, OutgoingEvent{
				Type: "permanent_error",
				Data: map[string]interface{}{"error": e.Err.Error()},
			})

		case *messagix.Event_Reconnected:
			log.Info().Msg("MQTT reconnected")
			writeEvent(stdout, OutgoingEvent{Type: "reconnected"})

		default:
			log.Info().Str("type", fmt.Sprintf("%T", evt)).Msg("Unhandled event type")
		}
	}
}

func (b *Bridge) Run(ctx context.Context, stdin io.Reader, stdout io.Writer) {
	ctx, cancel := context.WithCancel(ctx)
	b.cancel = cancel
	defer cancel()
	b.stdout = stdout
	b.startTime = time.Now()

	// Start HTTP API server for cookie uploads
	b.startHTTPServer(ctx)

	writeEvent(stdout, OutgoingEvent{Type: "log", Data: "Loading messages page..."})

	userInfo, _, err := b.client.LoadMessagesPage(ctx)
	if err != nil {
		log.Fatal().Err(err).Msg("Failed to load messages page")
	}

	b.uid = userInfo.GetFBID()
	userName := userInfo.GetName()
	log.Info().Int64("uid", b.uid).Str("name", userName).Msg("Logged in")

	writeEvent(stdout, OutgoingEvent{
		Type: "ready",
		Data: ReadyData{UID: b.uid, Name: userName},
	})

	writeEvent(stdout, OutgoingEvent{Type: "log", Data: "Connecting to MQTT..."})

	b.client.SetEventHandler(b.makeEventHandler(ctx, stdout))

	go func() {
		err := b.client.Connect(ctx)
		if err != nil && !errors.Is(err, context.Canceled) {
			log.Error().Err(err).Msg("Connection error")
		}
	}()

	sigCh := make(chan os.Signal, 1)
	signal.Notify(sigCh, syscall.SIGINT, syscall.SIGTERM)

	stdinReader := bufio.NewReader(stdin)
	stdinDone := make(chan struct{})
	go func() {
		defer close(stdinDone)
		for {
			line, err := stdinReader.ReadString('\n')
			if err != nil {
				if !errors.Is(err, io.EOF) {
					log.Error().Err(err).Msg("Stdin read error")
				}
				return
			}
			line = strings.TrimSpace(line)
			if line == "" {
				continue
			}

			var cmd IncomingCommand
			if err := json.Unmarshal([]byte(line), &cmd); err != nil {
				log.Error().Err(err).Str("line", line).Msg("Failed to parse command")
				continue
			}

			b.handleCommand(ctx, stdout, cmd)
		}
	}()

	select {
	case <-sigCh:
		log.Info().Msg("Signal received, shutting down")
	case <-ctx.Done():
		log.Info().Msg("Context cancelled, shutting down")
	}

	cancel()
	b.client.Disconnect()
	time.Sleep(500 * time.Millisecond)
}

func (b *Bridge) SendMessage(ctx context.Context, threadID int64, text string) string {
	if ch, ok := b.threadChannel[threadID]; ok && ch == "whatsapp" {
		jid := b.waJIDs[threadID]
		if jid != "" && b.cfg.WACLI_SEND_WEBHOOK_URL != "" {
			if err := b.postWacliSend(jid, text); err != nil {
				b.logger.Error().Err(err).Int64("thread_id", threadID).Str("jid", jid).Msg("wacli send webhook failed")
			} else {
				b.logger.Info().Int64("thread_id", threadID).Str("jid", jid).Msg("wacli send webhook ok")
				return ""
			}
		}
		if jid != "" && b.waClient != nil && b.waClient.IsConnected() {
			b.waClient.SendText(ctx, jid, text)
			return ""
		}
		// Fallback: store in outbox for reverse polling
		if jid != "" {
			b.outboxMu.Lock()
			b.outbox = append(b.outbox, map[string]string{"jid": jid, "text": text, "id": fmt.Sprintf("%d", time.Now().UnixNano())})
			b.outboxMu.Unlock()
			b.logger.Info().Int64("thread_id", threadID).Str("jid", jid).Msg("Stored reply in outbox for reverse polling")
		} else {
			b.logger.Warn().Int64("thread_id", threadID).Msg("WhatsApp client not connected and no JID for outbox")
		}
		return ""
	}

	otid := time.Now().UnixMilli()
	resp, err := b.client.ExecuteTasks(ctx, &socket.SendMessageTask{
		ThreadId:          threadID,
		Otid:              otid,
		Source:            table.MESSENGER_INBOX_IN_THREAD,
		SendType:          table.TEXT,
		Text:              text,
		SyncGroup:         1,
		InitiatingSource:  table.FACEBOOK_INBOX,
		SkipUrlPreviewGen: 1,
		MultiTabEnv:       0,
	})
	if err != nil {
		log.Error().Err(err).Msg("Failed to send AI reply")
		return ""
	}
	var msgID string
	if resp != nil {
		otidStr := fmt.Sprintf("%d", otid)
		for _, replace := range resp.LSReplaceOptimsiticMessage {
			if replace.OfflineThreadingId == otidStr {
				msgID = replace.MessageId
				break
			}
		}
	}
	return msgID
}

func (b *Bridge) postWacliSend(jid string, text string) error {
	payload, err := json.Marshal(map[string]string{
		"jid":  jid,
		"text": text,
	})
	if err != nil {
		return fmt.Errorf("marshal payload: %w", err)
	}
	ctx, cancel := context.WithTimeout(context.Background(), 30*time.Second)
	defer cancel()
	req, err := http.NewRequestWithContext(ctx, http.MethodPost, b.cfg.WACLI_SEND_WEBHOOK_URL, bytes.NewReader(payload))
	if err != nil {
		return fmt.Errorf("create request: %w", err)
	}
	req.Header.Set("Content-Type", "application/json")
	resp, err := http.DefaultClient.Do(req)
	if err != nil {
		// Store in outbox for reverse polling
		b.outboxMu.Lock()
		b.outbox = append(b.outbox, map[string]string{"jid": jid, "text": text, "id": fmt.Sprintf("%d", time.Now().UnixNano())})
		b.outboxMu.Unlock()
		return fmt.Errorf("do request: %w", err)
	}
	defer resp.Body.Close()
	if resp.StatusCode >= 400 {
		body, _ := io.ReadAll(resp.Body)
		// Store in outbox for reverse polling
		b.outboxMu.Lock()
		b.outbox = append(b.outbox, map[string]string{"jid": jid, "text": text, "id": fmt.Sprintf("%d", time.Now().UnixNano())})
		b.outboxMu.Unlock()
		return fmt.Errorf("webhook returned %d: %s", resp.StatusCode, string(body))
	}
	return nil
}

func (b *Bridge) EditMessage(ctx context.Context, threadID int64, messageID string, text string) error {
	if ch, ok := b.threadChannel[threadID]; ok && ch == "whatsapp" {
		if messageID == "" {
			b.SendMessage(ctx, threadID, text)
			return nil
		}
		// WhatsApp doesn't support edits — send a new message instead
		b.SendMessage(ctx, threadID, text)
		return nil
	}

	_, err := b.client.ExecuteTasks(ctx, &socket.EditMessageTask{
		MessageID: messageID,
		Text:      text,
	})
	if err != nil {
		log.Error().Err(err).Msg("Failed to edit message")
		return err
	}
	return nil
}

func (b *Bridge) editMessage(ctx context.Context, threadID int64, messageID string, text string) {
	_ = b.EditMessage(ctx, threadID, messageID, text)
}

func (b *Bridge) sendImage(ctx context.Context, threadID int64, imageData []byte, mimeType string) {
	resp, err := b.client.SendMercuryUploadRequest(ctx, threadID, &messagix.MercuryUploadMedia{
		Filename:  "image.png",
		MimeType:  mimeType,
		MediaData: imageData,
	})
	if err != nil {
		log.Error().Err(err).Msg("Failed to upload image")
		b.SendMessage(ctx, threadID, "[image upload failed]")
		return
	}
	attachmentID := resp.Payload.RealMetadata.GetFbId()
	if attachmentID == 0 {
		log.Error().Msg("No attachment FBID returned from upload")
		b.SendMessage(ctx, threadID, "[image upload returned no ID]")
		return
	}
	log.Info().Int64("attachment_id", attachmentID).Msg("Image uploaded successfully")

	otid := time.Now().UnixMilli()
	_, err = b.client.ExecuteTasks(ctx, &socket.SendMessageTask{
		ThreadId:          threadID,
		Otid:              otid,
		Source:            table.MESSENGER_INBOX_IN_THREAD,
		SendType:          table.MEDIA,
		AttachmentFBIds:   []int64{attachmentID},
		SyncGroup:         1,
		InitiatingSource:  table.FACEBOOK_INBOX,
		SkipUrlPreviewGen: 1,
		MultiTabEnv:       0,
	})
	if err != nil {
		log.Error().Err(err).Msg("Failed to send image message")
	}
}

func (b *Bridge) shouldRespond(msg *table.LSInsertMessage) bool {
	text := strings.TrimSpace(msg.Text)
	if strings.HasPrefix(text, "/ai") {
		return true
	}
	if msg.ReplySourceId != "" && msg.ReplyToUserId == b.uid {
		return true
	}
	if msg.MentionIds != "" {
		for _, idStr := range strings.Split(msg.MentionIds, ",") {
			idStr = strings.TrimSpace(idStr)
			if id, err := strconv.ParseInt(idStr, 10, 64); err == nil && id == b.uid {
				return true
			}
		}
	}
	return false
}

func (b *Bridge) cleanPrompt(text string, msg *table.LSInsertMessage) string {
	if msg.MentionIds == "" || msg.MentionOffsets == "" || msg.MentionLengths == "" {
		return text
	}
	ids := strings.Split(msg.MentionIds, ",")
	offsets := strings.Split(msg.MentionOffsets, ",")
	lengths := strings.Split(msg.MentionLengths, ",")
	runes := []rune(text)
	var toRemove [][2]int
	for i, idStr := range ids {
		idStr = strings.TrimSpace(idStr)
		id, err := strconv.ParseInt(idStr, 10, 64)
		if err != nil || id != b.uid {
			continue
		}
		if i >= len(offsets) || i >= len(lengths) {
			continue
		}
		off, err := strconv.Atoi(strings.TrimSpace(offsets[i]))
		if err != nil {
			continue
		}
		ln, err := strconv.Atoi(strings.TrimSpace(lengths[i]))
		if err != nil {
			continue
		}
		toRemove = append(toRemove, [2]int{off, off + ln})
	}
	if len(toRemove) == 0 {
		return text
	}
	for i := len(toRemove) - 1; i >= 0; i-- {
		start, end := toRemove[i][0], toRemove[i][1]
		if start >= 0 && end <= len(runes) {
			runes = append(runes[:start], runes[end:]...)
		}
	}
	return strings.TrimSpace(string(runes))
}

func (b *Bridge) handleAICommand(ctx context.Context, threadID int64, text string) {
	lower := strings.ToLower(strings.TrimSpace(text))

	if lower == "/ai" || lower == "/ai " || lower == "/ai help" {
		b.SendMessage(ctx, threadID, b.ai.Help())
		return
	}

	if lower == "/ai status" {
		model := b.threadModels[threadID]
		if model == "" {
			model = b.ai.DefaultChat
		}
		imgModel := b.threadImgModels[threadID]
		if imgModel == "" {
			imgModel = b.ai.DefaultImage
		}
		b.SendMessage(ctx, threadID, fmt.Sprintf("murmur\n\nplatform: messenger\nchat: %s\nimage: %s\nversion: 1.0.0", model, imgModel))
		return
	}

	if lower == "/ai reset" {
		delete(b.threadModels, threadID)
		delete(b.threadImgModels, threadID)
		if b.db != nil {
			if err := b.db.SetChatModel(ctx, threadID, ""); err != nil {
				log.Error().Err(err).Msg("Failed to reset chat model")
			}
			if err := b.db.SetImageModel(ctx, threadID, ""); err != nil {
				log.Error().Err(err).Msg("Failed to reset image model")
			}
		}
		b.SendMessage(ctx, threadID, "[model reset] chat and image models cleared for this thread")
		return
	}

	if strings.HasPrefix(lower, "/ai image models full") {
		parts := strings.Fields(text)
		page := ai.ParsePage(parts, 4)
		b.pendingImgModels[threadID] = b.ai.AllImageModels()
		b.SendMessage(ctx, threadID, b.ai.ListImageModels(page))
		return
	}
	if strings.HasPrefix(lower, "/ai image models") {
		parts := strings.Fields(text)
		page := ai.ParsePage(parts, 3)
		b.pendingImgModels[threadID] = b.ai.AllImageModels()
		b.SendMessage(ctx, threadID, b.ai.ListImageModels(page))
		return
	}
	if strings.HasPrefix(lower, "/ai models full") {
		parts := strings.Fields(text)
		page := ai.ParsePage(parts, 3)
		b.pendingModels[threadID] = b.ai.AllChatModels()
		b.SendMessage(ctx, threadID, b.ai.ListAllModels(page))
		return
	}
	if strings.HasPrefix(lower, "/ai models") {
		parts := strings.Fields(text)
		page := ai.ParsePage(parts, 2)
		b.pendingModels[threadID] = b.ai.AllChatModels()
		b.SendMessage(ctx, threadID, b.ai.ListModels(page))
		return
	}

	if strings.HasPrefix(lower, "/ai image model ") {
		parts := strings.Fields(text)
		if len(parts) >= 4 {
			if n, err := strconv.Atoi(parts[3]); err == nil && n > 0 {
				b.handleModelSelect(ctx, threadID, n, true)
				return
			}
		}
		b.SendMessage(ctx, threadID, "[usage] /ai image model <number>")
		return
	}

	if strings.HasPrefix(lower, "/ai model ") {
		parts := strings.Fields(text)
		if len(parts) >= 3 {
			if n, err := strconv.Atoi(parts[2]); err == nil && n > 0 {
				b.handleModelSelect(ctx, threadID, n, false)
				return
			}
		}
		b.SendMessage(ctx, threadID, "[usage] /ai model <number>")
		return
	}

	if strings.HasPrefix(lower, "/ai image ") {
		prompt := strings.TrimSpace(text[len("/ai image "):])
		imgModel := b.threadImgModels[threadID]
		msgID := b.SendMessage(ctx, threadID, "thinking.")
		stop := make(chan struct{})
		go func() {
			dots := []string{"thinking.", "thinking..", "thinking..."}
			i := 0
			edits := 0
			for edits < 4 {
				select {
				case <-stop:
					return
				case <-time.After(500 * time.Millisecond):
					i = (i + 1) % len(dots)
					if msgID != "" {
						b.editMessage(ctx, threadID, msgID, dots[i])
					}
					edits++
				}
			}
		}()
		imageData, mimeType, err := b.ai.ImageRawWithModel(prompt, imgModel)
		close(stop)
		if err != nil {
			b.SendMessage(ctx, threadID, fmt.Sprintf("[image error] %v", err))
			return
		}
		finalModel := imgModel
		if finalModel == "" {
			finalModel = b.ai.DefaultImage
		}
		if msgID != "" {
			b.editMessage(ctx, threadID, msgID, fmt.Sprintf("[%s]", finalModel))
		}
		if len(imageData) > 0 {
			b.sendImage(ctx, threadID, imageData, mimeType)
		} else {
			b.SendMessage(ctx, threadID, "[image generation returned no data]")
		}
		return
	}

	prompt := strings.TrimSpace(text[len("/ai "):])
	if prompt == "" {
		b.SendMessage(ctx, threadID, b.ai.Help())
		return
	}

	model := b.threadModels[threadID]

	// Load conversation history
	var history []ai.ChatMessage
	if b.db != nil {
		dbMsgs, err := b.db.GetRecentMessages(ctx, threadID, b.cfg.ContextWindow)
		if err != nil {
			log.Error().Err(err).Msg("Failed to load conversation history")
		} else {
			history = make([]ai.ChatMessage, len(dbMsgs))
			for i, m := range dbMsgs {
				history[i] = ai.ChatMessage{Role: m.Role, Content: m.Content}
			}
		}
	}

	msgID := b.SendMessage(ctx, threadID, "thinking.")
	log.Info().Str("prompt", prompt).Str("model", model).Int("history", len(history)).Msg("Chat request")
	stop := make(chan struct{})
	go func() {
		dots := []string{"thinking.", "thinking..", "thinking..."}
		i := 0
		edits := 0
		for edits < 4 {
			select {
			case <-stop:
				return
			case <-time.After(500 * time.Millisecond):
				i = (i + 1) % len(dots)
				if msgID != "" {
					b.editMessage(ctx, threadID, msgID, dots[i])
				}
				edits++
			}
		}
	}()
	resp, err := b.ai.ChatWithHistory(prompt, model, history)
	b.logger.Info().Str("resp_raw", resp).Err(err).Str("model_used", model).Int("history_len", len(history)).Msg("ChatWithHistory result in handleAICommand")
	close(stop)
	if err != nil {
		log.Error().Err(err).Msg("Chat error")
		b.SendMessage(ctx, threadID, fmt.Sprintf("[chat error] %v", err))
		return
	}
	if resp == "" {
		// defensive retry without history in case context/history causes empty response
		b.logger.Warn().Msg("Empty response from ChatWithHistory, retrying without history")
		resp, err = b.ai.ChatWithHistory(prompt, model, nil)
		if err != nil {
			b.SendMessage(ctx, threadID, fmt.Sprintf("[chat error on retry] %v", err))
			return
		}
		if resp == "" {
			b.SendMessage(ctx, threadID, "[error] model returned empty response twice")
			return
		}
	}
	log.Info().Str("resp", resp[:min(50, len(resp))]).Msg("Chat response")
	finalModel := model
	if finalModel == "" {
		finalModel = b.ai.DefaultChat
	}
	final := fmt.Sprintf("[%s]\n%s", finalModel, resp)
	b.logger.Info().Str("final_text", final).Msg("Final reply before sending")
	if msgID != "" {
		b.editMessage(ctx, threadID, msgID, final)
	} else {
		b.SendMessage(ctx, threadID, final)
	}

	// Save conversation history
	if b.db != nil {
		messages := append(history, ai.ChatMessage{Role: "user", Content: prompt})
		messages = append(messages, ai.ChatMessage{Role: "assistant", Content: resp})
		if err := b.db.SaveMessages(ctx, threadID, toDBMessages(messages)); err != nil {
			log.Error().Err(err).Msg("Failed to save conversation history")
		}
	}
}

func (b *Bridge) handleModelSelect(ctx context.Context, threadID int64, choice int, isImage bool) {
	if isImage {
		if models, ok := b.pendingImgModels[threadID]; ok && len(models) > 0 {
			if choice >= 1 && choice <= len(models) {
				selected := models[choice-1]
				b.threadImgModels[threadID] = selected
				delete(b.pendingImgModels, threadID)
				if b.db != nil {
					if err := b.db.SetImageModel(ctx, threadID, selected); err != nil {
						log.Error().Err(err).Msg("Failed to save image model")
					}
				}
				b.SendMessage(ctx, threadID, fmt.Sprintf("[image model set to %s]", selected))
				return
			}
			b.SendMessage(ctx, threadID, fmt.Sprintf("[invalid choice, pick 1-%d]", len(models)))
			return
		}
		b.SendMessage(ctx, threadID, "[no image models listed yet, run /ai image models first]")
		return
	}

	if models, ok := b.pendingModels[threadID]; ok && len(models) > 0 {
		if choice >= 1 && choice <= len(models) {
			selected := models[choice-1]
			b.threadModels[threadID] = selected
			delete(b.pendingModels, threadID)
			if b.db != nil {
				if err := b.db.SetChatModel(ctx, threadID, selected); err != nil {
					log.Error().Err(err).Msg("Failed to save chat model")
				}
			}
			b.SendMessage(ctx, threadID, fmt.Sprintf("[chat model set to %s]", selected))
			return
		}
		b.SendMessage(ctx, threadID, fmt.Sprintf("[invalid choice, pick 1-%d]", len(models)))
		return
	}
	b.SendMessage(ctx, threadID, "[no models listed yet, run /ai models first]")
}

func (b *Bridge) handleCommand(ctx context.Context, stdout io.Writer, cmd IncomingCommand) {
	switch cmd.Type {
	case "send_message":
		var data SendMessageData
		if err := json.Unmarshal(cmd.Data, &data); err != nil {
			log.Error().Err(err).Msg("Failed to parse send_message data")
			return
		}

		otid := time.Now().UnixMilli()
		_, err := b.client.ExecuteTasks(ctx, &socket.SendMessageTask{
			ThreadId:          data.ThreadID,
			Otid:              otid,
			Source:            table.MESSENGER_INBOX_IN_THREAD,
			SendType:          table.TEXT,
			Text:              data.Text,
			SyncGroup:         1,
			InitiatingSource:  table.FACEBOOK_INBOX,
			SkipUrlPreviewGen: 1,
			MultiTabEnv:       0,
		})
		if err != nil {
			log.Error().Err(err).Msg("Failed to send message")
			writeEvent(stdout, OutgoingEvent{
				Type: "error",
				Data: map[string]interface{}{
					"command": cmd.Type,
					"error":   err.Error(),
					"id":      cmd.ID,
				},
			})
		} else {
			writeEvent(stdout, OutgoingEvent{
				Type: "sent",
				Data: map[string]interface{}{
					"id":        cmd.ID,
					"otid":      otid,
					"thread_id": data.ThreadID,
				},
			})
		}

	case "edit_message":
		var data EditMessageData
		if err := json.Unmarshal(cmd.Data, &data); err != nil {
			log.Error().Err(err).Msg("Failed to parse edit_message data")
			return
		}
		_, err := b.client.ExecuteTasks(ctx, &socket.EditMessageTask{
			MessageID: data.MessageID,
			Text:      data.Text,
		})
		if err != nil {
			log.Error().Err(err).Msg("Failed to edit message")
			writeEvent(stdout, OutgoingEvent{
				Type: "error",
				Data: map[string]interface{}{
					"command": cmd.Type,
					"error":   err.Error(),
					"id":      cmd.ID,
				},
			})
		} else {
			writeEvent(stdout, OutgoingEvent{
				Type: "edited",
				Data: map[string]interface{}{
					"id":         cmd.ID,
					"message_id": data.MessageID,
				},
			})
		}

	case "delete_message":
		var data DeleteMessageData
		if err := json.Unmarshal(cmd.Data, &data); err != nil {
			log.Error().Err(err).Msg("Failed to parse delete_message data")
			return
		}
		_, err := b.client.ExecuteTasks(ctx, &socket.DeleteMessageTask{
			MessageId: data.MessageID,
		})
		if err != nil {
			log.Error().Err(err).Msg("Failed to delete message")
			writeEvent(stdout, OutgoingEvent{
				Type: "error",
				Data: map[string]interface{}{
					"command": cmd.Type,
					"error":   err.Error(),
					"id":      cmd.ID,
				},
			})
		} else {
			writeEvent(stdout, OutgoingEvent{
				Type: "deleted",
				Data: map[string]interface{}{
					"id":         cmd.ID,
					"message_id": data.MessageID,
				},
			})
		}

	case "get_uid":
		currentUser, err := b.client.GetCurrentAccount()
		if err != nil {
			log.Error().Err(err).Msg("Failed to get current account")
			return
		}
		writeEvent(stdout, OutgoingEvent{
			Type: "uid",
			Data: map[string]interface{}{
				"id":   cmd.ID,
				"uid":  currentUser.GetFBID(),
				"name": currentUser.GetName(),
			},
		})

	case "stop":
		log.Info().Msg("Stop requested")
		b.cancel()

	default:
		log.Warn().Str("type", cmd.Type).Msg("Unknown command type")
	}
}

func toDBMessages(msgs []ai.ChatMessage) []database.Message {
	out := make([]database.Message, len(msgs))
	for i, m := range msgs {
		out[i] = database.Message{Role: m.Role, Content: m.Content}
	}
	return out
}

func (b *Bridge) HandleWhatsAppMessage(ctx context.Context, msg whatsapp.ParsedMessage) {
	if msg.FromMe {
		return
	}
	if msg.Text == "" && msg.Media == nil && msg.Poll == nil {
		return
	}

	chatJID := msg.Chat.String()
	threadID := whatsapp.JIDToThreadID(chatJID)

	b.threadChannel[threadID] = "whatsapp"
	b.waJIDs[threadID] = chatJID

	b.logger.Info().
		Str("chat", chatJID).
		Str("sender", msg.SenderJID).
		Str("text", truncateStr(msg.Text, 100)).
		Int64("thread_id", threadID).
		Msg("Processing WhatsApp message")

	text := msg.Text
	if text == "" {
		if msg.Media != nil {
			text = fmt.Sprintf("[media: %s]", msg.Media.Type)
		} else if msg.Poll != nil {
			text = fmt.Sprintf("Poll: %s", msg.Poll.Question)
		}
	}

	if !b.shouldRespondWhatsApp(text) {
		return
	}

	if strings.HasPrefix(strings.TrimSpace(text), "/ai") {
		b.handleAICommand(ctx, threadID, text)
	} else {
		cleaned := strings.TrimSpace(text)
		if cleaned != "" {
			b.handleAICommand(ctx, threadID, "/ai "+cleaned)
		}
	}
}

func (b *Bridge) shouldRespondWhatsApp(text string) bool {
	text = strings.TrimSpace(text)
	if strings.HasPrefix(text, "/ai") {
		return true
	}
	return false
}

func (b *Bridge) SendMessageWhatsApp(ctx context.Context, chatJID string, text string) {
	if b.waSender == nil {
		b.logger.Warn().Msg("WhatsApp sender not configured")
		return
	}
	if err := b.waSender.SendText(ctx, chatJID, text); err != nil {
		b.logger.Error().Err(err).Msg("Failed to send WhatsApp message")
	}
}

func truncateStr(s string, max int) string {
	if len(s) <= max {
		return s
	}
	return s[:max] + "..."
}

func getPort() string {
	if p := os.Getenv("PORT"); p != "" {
		return p
	}
	return "7860"
}

func (b *Bridge) startHTTPServer(ctx context.Context) {
	mux := http.NewServeMux()
	mux.HandleFunc("/api/cookies/upload", b.handleCookieUpload)
	mux.HandleFunc("/api/health", func(w http.ResponseWriter, r *http.Request) {
		w.WriteHeader(http.StatusOK)
		w.Write([]byte("ok"))
	})
	mux.HandleFunc("/api/send_message", b.handleSendMessage)
	mux.HandleFunc("/api/automation/notifications", b.handleAutomationNotification)
	mux.HandleFunc("/api/version", func(w http.ResponseWriter, r *http.Request) {
		w.Header().Set("Content-Type", "application/json")
		w.WriteHeader(http.StatusOK)
		w.Write([]byte(`{"version":"1.0.1","fix":"extractChatContent","build":"2026-07-05"}`))
	})
	mux.HandleFunc("/api/debug/chat", func(w http.ResponseWriter, r *http.Request) {
		if r.Method != http.MethodGet {
			http.Error(w, "GET only", http.StatusMethodNotAllowed)
			return
		}
		prompt := r.URL.Query().Get("prompt")
		if prompt == "" {
			prompt = "hello"
		}
		model := r.URL.Query().Get("model")
		if model == "" {
			model = b.ai.DefaultChat
		}
		resp, err := b.ai.ChatWithHistory(prompt, model, nil)
		w.Header().Set("Content-Type", "application/json")
		if err != nil {
			w.WriteHeader(http.StatusOK)
			json.NewEncoder(w).Encode(map[string]interface{}{
				"error": err.Error(),
				"base_url": b.ai.BaseURL,
				"default_chat": b.ai.DefaultChat,
				"requested_model": model,
			})
			return
		}
		json.NewEncoder(w).Encode(map[string]interface{}{
			"response": resp,
			"base_url": b.ai.BaseURL,
			"default_chat": b.ai.DefaultChat,
			"requested_model": model,
		})
	})
	mux.HandleFunc("/api/debug/whatsapp-chat", func(w http.ResponseWriter, r *http.Request) {
		if r.Method != http.MethodGet {
			http.Error(w, "GET only", http.StatusMethodNotAllowed)
			return
		}
		threadIDStr := r.URL.Query().Get("thread_id")
		threadID := int64(12345)
		if threadIDStr != "" {
			if n, err := strconv.ParseInt(threadIDStr, 10, 64); err == nil {
				threadID = n
			}
		}
		jid := r.URL.Query().Get("jid")
		if jid != "" {
			threadID = whatsapp.JIDToThreadID(jid)
		}
		prompt := r.URL.Query().Get("prompt")
		if prompt == "" {
			prompt = "tell me a joke"
		}
		model := b.threadModels[threadID]
		if model == "" {
			model = b.ai.DefaultChat
		}
		var history []ai.ChatMessage
		if b.db != nil {
			dbMsgs, err := b.db.GetRecentMessages(r.Context(), threadID, b.cfg.ContextWindow)
			if err != nil {
				log.Error().Err(err).Msg("Failed to load conversation history")
			} else {
				history = make([]ai.ChatMessage, len(dbMsgs))
				for i, m := range dbMsgs {
					history[i] = ai.ChatMessage{Role: m.Role, Content: m.Content}
				}
			}
		}
		resp, err := b.ai.ChatWithHistory(prompt, model, history)
		w.Header().Set("Content-Type", "application/json")
		json.NewEncoder(w).Encode(map[string]interface{}{
			"response":    resp,
			"error":       fmt.Sprintf("%v", err),
			"model":       model,
			"thread_id":   threadID,
			"history_len": len(history),
			"history":     history,
		})
	})
	mux.HandleFunc("/api/debug/raw", func(w http.ResponseWriter, r *http.Request) {
		if r.Method != http.MethodGet {
			http.Error(w, "GET only", http.StatusMethodNotAllowed)
			return
		}
		raw := ai.GetLastDebugRawResponse()
		w.Header().Set("Content-Type", "application/json")
		w.Write(raw)
	})
	mux.HandleFunc("/api/debug/outbox", func(w http.ResponseWriter, r *http.Request) {
		if r.Method != http.MethodGet {
			http.Error(w, "GET only", http.StatusMethodNotAllowed)
			return
		}
		b.outboxMu.Lock()
		msgs := make([]map[string]string, len(b.outbox))
		copy(msgs, b.outbox)
		b.outboxMu.Unlock()
		w.Header().Set("Content-Type", "application/json")
		json.NewEncoder(w).Encode(map[string]interface{}{"messages": msgs, "count": len(msgs)})
	})
	mux.HandleFunc("/", func(w http.ResponseWriter, r *http.Request) {
		if r.URL.Path != "/" {
			http.NotFound(w, r)
			return
		}
		w.WriteHeader(http.StatusOK)
		w.Write([]byte("murmur"))
	})

	// WhatsApp webhook endpoint is always available (local wacli sends to this)
	mux.HandleFunc("/wacli/webhook", b.handleWhatsAppWebhook)
	mux.HandleFunc("/api/wacli/session", b.handleWacliSessionUpload)
	mux.HandleFunc("/api/outbox", b.handleOutbox)
	mux.HandleFunc("/api/outbox/ack", b.handleOutboxAck)

	// Only start built-in whatsmeow client if explicitly enabled (disabled by default, use webhook instead)
	if b.cfg.WhatsAppEnabled {
		log.Info().Msg("WhatsApp built-in client enabled (WHATSAPP_ENABLED=1)")
	}

	b.httpServer = &http.Server{
		Addr:    ":" + getPort(),
		Handler: mux,
	}

	go func() {
		log.Info().Msg("Starting HTTP API server on :7860")
		if err := b.httpServer.ListenAndServe(); err != nil && !errors.Is(err, http.ErrServerClosed) {
			log.Error().Err(err).Msg("HTTP server error")
		}
	}()

	go func() {
		<-ctx.Done()
		log.Info().Msg("Shutting down HTTP server")
		ctx, cancel := context.WithTimeout(context.Background(), 5*time.Second)
		defer cancel()
		b.httpServer.Shutdown(ctx)
	}()
}

func (b *Bridge) handleCookieUpload(w http.ResponseWriter, r *http.Request) {
	if r.Method != http.MethodPost {
		http.Error(w, "Method not allowed", http.StatusMethodNotAllowed)
		return
	}

	var cookieMap cookies.CookieMap
	if err := json.NewDecoder(r.Body).Decode(&cookieMap); err != nil {
		http.Error(w, "Invalid JSON: "+err.Error(), http.StatusBadRequest)
		return
	}

	// Write to file
	data, err := json.MarshalIndent(cookieMap, "", "  ")
	if err != nil {
		http.Error(w, "Failed to marshal cookies: "+err.Error(), http.StatusInternalServerError)
		return
	}

	if err := os.WriteFile(b.cfg.CookiesPath, data, 0644); err != nil {
		http.Error(w, "Failed to write cookies file: "+err.Error(), http.StatusInternalServerError)
		return
	}

	// Reload cookies in bridge
	if err := b.ReloadCookies(r.Context()); err != nil {
		http.Error(w, "Failed to reload cookies: "+err.Error(), http.StatusInternalServerError)
		return
	}

	w.Header().Set("Content-Type", "application/json")
	json.NewEncoder(w).Encode(map[string]string{"status": "ok", "message": "Cookies uploaded and bridge reloaded"})
}

func (b *Bridge) handleSendMessage(w http.ResponseWriter, r *http.Request) {
	if r.Method != http.MethodPost {
		http.Error(w, "Method not allowed", http.StatusMethodNotAllowed)
		return
	}

	var req struct {
		ThreadID string `json:"thread_id"`
		Text     string `json:"text"`
	}
	if err := json.NewDecoder(r.Body).Decode(&req); err != nil {
		http.Error(w, "Invalid JSON: "+err.Error(), http.StatusBadRequest)
		return
	}

	threadID, err := strconv.ParseInt(req.ThreadID, 10, 64)
	if err != nil {
		http.Error(w, "Invalid thread_id", http.StatusBadRequest)
		return
	}

	b.SendMessage(r.Context(), threadID, req.Text)

	w.Header().Set("Content-Type", "application/json")
	json.NewEncoder(w).Encode(map[string]string{"status": "ok"})
}

func (b *Bridge) handleAutomationNotification(w http.ResponseWriter, r *http.Request) {
	if r.Method != http.MethodPost {
		http.Error(w, "Method not allowed", http.StatusMethodNotAllowed)
		return
	}

	var req struct {
		Source    string `json:"source"`
		ThreadID  string `json:"threadId"`
		Title     string `json:"title"`
		Message   string `json:"message"`
		DedupeKey string `json:"dedupeKey"`
		URL       string `json:"url"`
	}
	if err := json.NewDecoder(r.Body).Decode(&req); err != nil {
		http.Error(w, "Invalid JSON: "+err.Error(), http.StatusBadRequest)
		return
	}

	threadIDStr := req.ThreadID
	if threadIDStr == "" {
		threadIDStr = automationNotificationDefaultThreadID()
	}
	threadID, err := strconv.ParseInt(threadIDStr, 10, 64)
	if err != nil {
		http.Error(w, "Invalid threadId", http.StatusBadRequest)
		return
	}

	text := strings.TrimSpace(req.Message)
	if text == "" {
		text = strings.TrimSpace(req.Title)
	} else if title := strings.TrimSpace(req.Title); title != "" {
		text = title + "\n\n" + text
	}
	if text == "" {
		http.Error(w, "message is required", http.StatusBadRequest)
		return
	}

	b.SendMessage(r.Context(), threadID, text)

	notifID := fmt.Sprintf("ntf_%d", time.Now().UnixMilli())
	w.Header().Set("Content-Type", "application/json")
	json.NewEncoder(w).Encode(map[string]interface{}{
		"id":     notifID,
		"status": "sent",
	})
}

func automationNotificationDefaultThreadID() string {
	if v := os.Getenv("MURMUR_AUTOMATION_NOTIFICATION_DEFAULT_THREAD_ID"); v != "" {
		return v
	}
	return "2637078310061988"
}

func (b *Bridge) handleWhatsAppWebhook(w http.ResponseWriter, r *http.Request) {
	if r.Method != http.MethodPost {
		http.Error(w, "Method not allowed", http.StatusMethodNotAllowed)
		return
	}

	body, err := io.ReadAll(r.Body)
	if err != nil {
		http.Error(w, "Failed to read body", http.StatusBadRequest)
		return
	}

	if b.cfg.WhatsAppWebhookSecret != "" {
		sig := r.Header.Get("X-Wacli-Signature")
		if !whatsapp.VerifySignature(body, sig, b.cfg.WhatsAppWebhookSecret) {
			b.logger.Warn().Str("sig", sig).Msg("Invalid WhatsApp webhook signature")
			http.Error(w, "Invalid signature", http.StatusUnauthorized)
			return
		}
	}

	var msg whatsapp.ParsedMessage
	if err := json.Unmarshal(body, &msg); err != nil {
		b.logger.Error().Err(err).Msg("Failed to parse WhatsApp webhook payload")
		http.Error(w, "Invalid JSON", http.StatusBadRequest)
		return
	}

	b.HandleWhatsAppMessage(r.Context(), msg)

	w.Header().Set("Content-Type", "application/json")
	w.Write([]byte(`{"status":"ok"}`))
}

func (b *Bridge) handleWacliSessionUpload(w http.ResponseWriter, r *http.Request) {
	if r.Method != http.MethodPost {
		http.Error(w, "Method not allowed", http.StatusMethodNotAllowed)
		return
	}

	storeDir := b.cfg.WhatsAppStore
	if storeDir == "" {
		home, _ := os.UserHomeDir()
		storeDir = home + "/.wacli"
	}

	if err := os.MkdirAll(storeDir, 0700); err != nil {
		http.Error(w, "Failed to create store dir: "+err.Error(), http.StatusInternalServerError)
		return
	}

	contentType := r.Header.Get("Content-Type")

	if strings.HasPrefix(contentType, "multipart/form-data") {
		if err := r.ParseMultipartForm(32 << 20); err != nil {
			http.Error(w, "Failed to parse form: "+err.Error(), http.StatusBadRequest)
			return
		}

		for _, fileHeader := range r.MultipartForm.File["file"] {
			src, err := fileHeader.Open()
			if err != nil {
				http.Error(w, "Failed to open file: "+err.Error(), http.StatusInternalServerError)
				return
			}
			defer src.Close()

			filename := fileHeader.Filename
			if filename == "" {
				continue
			}

			dstPath := storeDir + "/" + filename
			dst, err := os.Create(dstPath)
			if err != nil {
				http.Error(w, "Failed to create file: "+err.Error(), http.StatusInternalServerError)
				return
			}
			defer dst.Close()

			if _, err := io.Copy(dst, src); err != nil {
				http.Error(w, "Failed to write file: "+err.Error(), http.StatusInternalServerError)
				return
			}

			b.logger.Info().Str("path", dstPath).Int64("size", fileHeader.Size).Msg("Wacli session file uploaded")
		}
	} else {
		body, err := io.ReadAll(r.Body)
		if err != nil {
			http.Error(w, "Failed to read body", http.StatusBadRequest)
			return
		}

		filename := r.Header.Get("X-Filename")
		if filename == "" {
			filename = "session.db"
		}

		dstPath := storeDir + "/" + filename
		if err := os.WriteFile(dstPath, body, 0600); err != nil {
			http.Error(w, "Failed to write file: "+err.Error(), http.StatusInternalServerError)
			return
		}

		b.logger.Info().Str("path", dstPath).Int("size", len(body)).Msg("Wacli session file uploaded")
	}

	// Also save session to Neon database for persistence across restarts
	if b.db != nil {
		sessionPath := storeDir + "/session.db"
		wacliPath := storeDir + "/wacli.db"
		sessionBytes, _ := os.ReadFile(sessionPath)
		wacliBytes, _ := os.ReadFile(wacliPath)
		if len(sessionBytes) > 0 {
			if err := b.db.SaveWhatsAppSession(r.Context(), sessionBytes, wacliBytes); err != nil {
				b.logger.Error().Err(err).Msg("Failed to save WhatsApp session to database")
			} else {
				b.logger.Info().Msg("WhatsApp session saved to database")
			}
		}
	}

	if b.waSync != nil {
		if err := b.waSync.Restart(); err != nil {
			b.logger.Error().Err(err).Msg("Failed to restart wacli sync after session upload")
			w.Header().Set("Content-Type", "application/json")
			json.NewEncoder(w).Encode(map[string]string{"status": "ok", "store": storeDir, "restart": "failed", "error": err.Error()})
			return
		}
		b.logger.Info().Msg("Wacli sync restarted after session upload")
	}

	w.Header().Set("Content-Type", "application/json")
	json.NewEncoder(w).Encode(map[string]string{"status": "ok", "store": storeDir, "restart": "ok"})
}

func (b *Bridge) handleOutbox(w http.ResponseWriter, r *http.Request) {
	if r.Method != http.MethodGet {
		http.Error(w, "Method not allowed", http.StatusMethodNotAllowed)
		return
	}
	b.outboxMu.Lock()
	msgs := make([]map[string]string, len(b.outbox))
	copy(msgs, b.outbox)
	b.outboxMu.Unlock()
	w.Header().Set("Content-Type", "application/json")
	json.NewEncoder(w).Encode(map[string]interface{}{"messages": msgs})
}

func (b *Bridge) handleOutboxAck(w http.ResponseWriter, r *http.Request) {
	if r.Method != http.MethodPost {
		http.Error(w, "Method not allowed", http.StatusMethodNotAllowed)
		return
	}
	var req struct {
		IDs []string `json:"ids"`
	}
	if err := json.NewDecoder(r.Body).Decode(&req); err != nil {
		http.Error(w, "Invalid JSON", http.StatusBadRequest)
		return
	}
	b.outboxMu.Lock()
	remaining := make([]map[string]string, 0, len(b.outbox))
	idSet := make(map[string]bool)
	for _, id := range req.IDs {
		idSet[id] = true
	}
	for _, msg := range b.outbox {
		if !idSet[msg["id"]] {
			remaining = append(remaining, msg)
		}
	}
	b.outbox = remaining
	b.outboxMu.Unlock()
	w.Header().Set("Content-Type", "application/json")
	json.NewEncoder(w).Encode(map[string]interface{}{"acked": len(req.IDs)})
}
