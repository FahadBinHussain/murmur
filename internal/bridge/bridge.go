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
	cfg    *config.Config
	client *messagix.Client
	ai     *ai.Client
	logger zerolog.Logger
	cancel context.CancelFunc
	uid    int64
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
		cfg:    cfg,
		client: client,
		ai:     ai.NewClient(cfg.LiteLLMBase, cfg.DefaultChat, cfg.DefaultImage),
		logger: logger,
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
						reply := b.ai.HandleCommand(msg.Text)
						b.sendMessage(ctx, msg.ThreadKey, reply)
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
							reply := b.ai.HandleCommand(msg.Text)
							b.sendMessage(ctx, threadID, reply)
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

func (b *Bridge) sendMessage(ctx context.Context, threadID int64, text string) {
	otid := time.Now().UnixMilli()
	_, err := b.client.ExecuteTasks(ctx, &socket.SendMessageTask{
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
	}
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