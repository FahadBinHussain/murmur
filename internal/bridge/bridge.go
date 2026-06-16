package bridge

import (
	"bufio"
	"context"
	"encoding/json"
	"errors"
	"fmt"
	"io"
	"os"
	"os/signal"
	"strconv"
	"strings"
	"syscall"
	"time"

	"github.com/rs/zerolog"
	"github.com/rs/zerolog/log"

	"github.com/user/murmur/internal/ai"
	"github.com/user/murmur/internal/config"
	"github.com/user/murmur/internal/cookies"

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
	cfg            *config.Config
	client         *messagix.Client
	ai             *ai.Client
	logger         zerolog.Logger
	cancel         context.CancelFunc
	uid            int64
	threadModels   map[int64]string
	threadImgModels map[int64]string
	pendingModels  map[int64][]string
	pendingImgModels map[int64][]string
}

func writeEvent(w io.Writer, evt OutgoingEvent) {
	data, err := json.Marshal(evt)
	if err != nil {
		log.Error().Err(err).Msg("Failed to marshal event")
		return
	}
	fmt.Fprintln(w, string(data))
}

func New(cfg *config.Config) *Bridge {
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

	return &Bridge{
		cfg:              cfg,
		client:           client,
		ai:               ai.NewClient(cfg.LiteLLMBase, cfg.DefaultChat, cfg.DefaultImage),
		logger:           logger,
		threadModels:     make(map[int64]string),
		threadImgModels:  make(map[int64]string),
		pendingModels:    make(map[int64][]string),
		pendingImgModels: make(map[int64][]string),
	}
}

func (b *Bridge) Run(ctx context.Context, stdin io.Reader, stdout io.Writer) {
	ctx, cancel := context.WithCancel(ctx)
	b.cancel = cancel
	defer cancel()

	startTime := time.Now()

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

	b.client.SetEventHandler(func(evtCtx context.Context, evt any) {
		switch e := evt.(type) {
		case *messagix.Event_Ready:
			log.Info().Any("connection_code", e.ConnectionCode).Msg("MQTT connected")
			writeEvent(stdout, OutgoingEvent{Type: "mqtt_connected"})
			// Start periodic foreground state keepalive
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
				return
			}
			upsertMessages, insertMessages := e.Table.WrapMessages()

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
				writeEvent(stdout, OutgoingEvent{Type: "message", Data: evt})

				if msg.SenderId != b.uid {
					ts := time.UnixMilli(msg.TimestampMs)
					if ts.Before(startTime.Add(-5 * time.Second)) {
						continue
					}
					if strings.HasPrefix(strings.TrimSpace(msg.Text), "/ai") {
						b.handleAICommand(ctx, msg.ThreadKey, msg.Text)
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
					writeEvent(stdout, OutgoingEvent{Type: "message", Data: evt})

					if msg.SenderId != b.uid {
						ts := time.UnixMilli(msg.TimestampMs)
						if ts.Before(startTime.Add(-5 * time.Second)) {
							continue
						}
						if strings.HasPrefix(strings.TrimSpace(msg.Text), "/ai") {
							b.handleAICommand(ctx, threadID, msg.Text)
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
	})

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

func (b *Bridge) sendMessage(ctx context.Context, threadID int64, text string) string {
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

func (b *Bridge) editMessage(ctx context.Context, messageID string, text string) {
	_, err := b.client.ExecuteTasks(ctx, &socket.EditMessageTask{
		MessageID: messageID,
		Text:      text,
	})
	if err != nil {
		log.Error().Err(err).Msg("Failed to edit message")
	}
}

func (b *Bridge) sendImage(ctx context.Context, threadID int64, imageData []byte, mimeType string) {
	resp, err := b.client.SendMercuryUploadRequest(ctx, threadID, &messagix.MercuryUploadMedia{
		Filename:  "image.png",
		MimeType:  mimeType,
		MediaData: imageData,
	})
	if err != nil {
		log.Error().Err(err).Msg("Failed to upload image")
		b.sendMessage(ctx, threadID, "[image upload failed]")
		return
	}
	attachmentID := resp.Payload.RealMetadata.GetFbId()
	if attachmentID == 0 {
		log.Error().Msg("No attachment FBID returned from upload")
		b.sendMessage(ctx, threadID, "[image upload returned no ID]")
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

func (b *Bridge) handleAICommand(ctx context.Context, threadID int64, text string) {
	lower := strings.ToLower(strings.TrimSpace(text))

	if lower == "/ai" || lower == "/ai " || lower == "/ai help" {
		b.sendMessage(ctx, threadID, b.ai.Help())
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
		b.sendMessage(ctx, threadID, fmt.Sprintf("murmur\n\nplatform: messenger\nchat: %s\nimage: %s\nversion: 1.0.0", model, imgModel))
		return
	}

	if strings.HasPrefix(lower, "/ai image models full") {
		parts := strings.Fields(text)
		page := ai.ParsePage(parts, 4)
		text, models := b.ai.ListImageModelsWithList(page)
		b.pendingImgModels[threadID] = models
		b.sendMessage(ctx, threadID, text)
		return
	}
	if strings.HasPrefix(lower, "/ai image models") {
		parts := strings.Fields(text)
		page := ai.ParsePage(parts, 3)
		text, models := b.ai.ListImageModelsWithList(page)
		b.pendingImgModels[threadID] = models
		b.sendMessage(ctx, threadID, text)
		return
	}
	if strings.HasPrefix(lower, "/ai models full") {
		parts := strings.Fields(text)
		page := ai.ParsePage(parts, 3)
		b.sendMessage(ctx, threadID, b.ai.ListAllModels(page))
		return
	}
	if strings.HasPrefix(lower, "/ai models") {
		parts := strings.Fields(text)
		page := ai.ParsePage(parts, 2)
		text, models := b.ai.ListModelsWithList(page)
		b.pendingModels[threadID] = models
		b.sendMessage(ctx, threadID, text)
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
		b.sendMessage(ctx, threadID, "[usage] /ai image model <number>")
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
		b.sendMessage(ctx, threadID, "[usage] /ai model <number>")
		return
	}

	if strings.HasPrefix(lower, "/ai image ") {
		prompt := strings.TrimSpace(text[len("/ai image "):])
		imgModel := b.threadImgModels[threadID]
		msgID := b.sendMessage(ctx, threadID, "thinking.")
		stop := make(chan struct{})
		go func() {
			dots := []string{"thinking.", "thinking..", "thinking..."}
			i := 0
			for {
				select {
				case <-stop:
					return
				case <-time.After(500 * time.Millisecond):
					i = (i + 1) % len(dots)
					if msgID != "" {
						b.editMessage(ctx, msgID, dots[i])
					}
				}
			}
		}()
		imageData, mimeType, err := b.ai.ImageRawWithModel(prompt, imgModel)
		close(stop)
		if err != nil {
			b.sendMessage(ctx, threadID, fmt.Sprintf("[image error] %v", err))
			return
		}
		if len(imageData) > 0 {
			b.sendImage(ctx, threadID, imageData, mimeType)
		} else {
			b.sendMessage(ctx, threadID, "[image generation returned no data]")
		}
		return
	}

	prompt := strings.TrimSpace(text[len("/ai "):])
	if prompt == "" {
		b.sendMessage(ctx, threadID, b.ai.Help())
		return
	}

	model := b.threadModels[threadID]
	msgID := b.sendMessage(ctx, threadID, "thinking.")
	stop := make(chan struct{})
	go func() {
		dots := []string{"thinking.", "thinking..", "thinking..."}
		i := 0
		for {
			select {
			case <-stop:
				return
			case <-time.After(500 * time.Millisecond):
				i = (i + 1) % len(dots)
				if msgID != "" {
					b.editMessage(ctx, msgID, dots[i])
				}
			}
		}
	}()
	resp, err := b.ai.ChatWithModel(prompt, model)
	close(stop)
	if err != nil {
		b.sendMessage(ctx, threadID, fmt.Sprintf("[chat error] %v", err))
		return
	}
	finalModel := model
	if finalModel == "" {
		finalModel = b.ai.DefaultChat
	}
	final := fmt.Sprintf("[%s]\n%s", finalModel, resp)
	if msgID != "" {
		b.editMessage(ctx, msgID, final)
	} else {
		b.sendMessage(ctx, threadID, final)
	}
}

func (b *Bridge) handleModelSelect(ctx context.Context, threadID int64, choice int, isImage bool) {
	if isImage {
		if models, ok := b.pendingImgModels[threadID]; ok && len(models) > 0 {
			if choice >= 1 && choice <= len(models) {
				selected := models[choice-1]
				b.threadImgModels[threadID] = selected
				delete(b.pendingImgModels, threadID)
				b.sendMessage(ctx, threadID, fmt.Sprintf("[image model set to %s]", selected))
				return
			}
			b.sendMessage(ctx, threadID, fmt.Sprintf("[invalid choice, pick 1-%d]", len(models)))
			return
		}
		b.sendMessage(ctx, threadID, "[no image models listed yet, run /ai image models first]")
		return
	}

	if models, ok := b.pendingModels[threadID]; ok && len(models) > 0 {
		if choice >= 1 && choice <= len(models) {
			selected := models[choice-1]
			b.threadModels[threadID] = selected
			delete(b.pendingModels, threadID)
			b.sendMessage(ctx, threadID, fmt.Sprintf("[chat model set to %s]", selected))
			return
		}
		b.sendMessage(ctx, threadID, fmt.Sprintf("[invalid choice, pick 1-%d]", len(models)))
		return
	}
	b.sendMessage(ctx, threadID, "[no models listed yet, run /ai models first]")
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