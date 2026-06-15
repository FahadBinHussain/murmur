package main

import (
	"bufio"
	"context"
	"encoding/json"
	"errors"
	"flag"
	"fmt"
	"io"
	"os"
	"os/signal"
	"strings"
	"syscall"
	"time"

	"github.com/rs/zerolog"
	"github.com/rs/zerolog/log"

	"go.mau.fi/mautrix-meta/pkg/messagix"
	"go.mau.fi/mautrix-meta/pkg/messagix/cookies"
	"go.mau.fi/mautrix-meta/pkg/messagix/socket"
	"go.mau.fi/mautrix-meta/pkg/messagix/table"
	"go.mau.fi/mautrix-meta/pkg/messagix/types"
)

type OutgoingEvent struct {
	Type string      `json:"type"`
	Data interface{} `json:"data,omitempty"`
}

type IncomingCommand struct {
	Type     string          `json:"type"`
	Data     json.RawMessage `json:"data,omitempty"`
	ID       string          `json:"id,omitempty"`
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
	MessageID  string `json:"message_id"`
	ThreadID   int64  `json:"thread_id"`
	SenderID   int64  `json:"sender_id"`
	Text       string `json:"text"`
	Timestamp  int64  `json:"timestamp"`
	IsUnsent   bool   `json:"is_unsent,omitempty"`
}

type ReadyData struct {
	UID  int64  `json:"uid"`
	Name string `json:"name"`
}

func writeEvent(w io.Writer, evt OutgoingEvent) {
	data, err := json.Marshal(evt)
	if err != nil {
		log.Error().Err(err).Msg("Failed to marshal event")
		return
	}
	fmt.Fprintln(w, string(data))
}

func main() {
	cookiesPath := flag.String("cookies", "", "Path to cookies JSON file")
	platform := flag.String("platform", "messenger", "Platform: messenger, facebook, messenger-lite")
	flag.Parse()

	zerolog.TimeFieldFormat = zerolog.TimeFormatUnixMs
	log.Logger = log.Output(zerolog.NewConsoleWriter(func(w *zerolog.ConsoleWriter) {
		w.NoColor = true
		w.Out = os.Stderr
	}))

	cookieFile := *cookiesPath
	if cookieFile == "" {
		cookieFile = os.Getenv("MESSENGER_COOKIES")
	}
	if cookieFile == "" {
		log.Fatal().Msg("No cookies file specified. Use --cookies flag or MESSENGER_COOKIES env var")
	}

	rawCookies, err := os.ReadFile(cookieFile)
	if err != nil {
		log.Fatal().Err(err).Msg("Failed to read cookies file")
	}

	var cookieMap map[string]string
	if err := json.Unmarshal(rawCookies, &cookieMap); err != nil {
		log.Fatal().Err(err).Msg("Failed to parse cookies JSON")
	}

	var plat types.Platform
	switch strings.ToLower(*platform) {
	case "messenger":
		plat = types.Messenger
	case "facebook":
		plat = types.Facebook
	case "messenger-lite":
		plat = types.MessengerLite
	default:
		plat = types.Messenger
	}

	c := &cookies.Cookies{Platform: plat}
	cookieMapTyped := make(map[cookies.MetaCookieName]string)
	for k, v := range cookieMap {
		cookieMapTyped[cookies.MetaCookieName(k)] = v
	}
	c.UpdateValues(cookieMapTyped)

	missing := c.GetMissingCookieNames()
	if len(missing) > 0 {
		log.Fatal().Strs("missing", func() []string {
			s := make([]string, len(missing))
			for i, n := range missing {
				s[i] = string(n)
			}
			return s
		}()).Msg("Missing required cookies")
	}

	ctx, cancel := context.WithCancel(context.Background())
	defer cancel()

	logger := log.Logger.With().Str("component", "messagix").Logger()

	client := messagix.NewClient(c, logger, &messagix.Config{
		MayConnectToDGW: false,
	})

	writeEvent(os.Stdout, OutgoingEvent{Type: "log", Data: "Loading messages page..."})

	userInfo, _, err := client.LoadMessagesPage(ctx)
	if err != nil {
		log.Fatal().Err(err).Msg("Failed to load messages page")
	}

	userID := userInfo.GetFBID()
	userName := userInfo.GetName()
	log.Info().Int64("uid", userID).Str("name", userName).Msg("Logged in")

	writeEvent(os.Stdout, OutgoingEvent{
		Type: "ready",
		Data: ReadyData{UID: userID, Name: userName},
	})

	writeEvent(os.Stdout, OutgoingEvent{Type: "log", Data: "Connecting to MQTT..."})

	client.SetEventHandler(func(evtCtx context.Context, evt any) {
		switch e := evt.(type) {
		case *messagix.Event_Ready:
			log.Info().Any("connection_code", e.ConnectionCode).Msg("MQTT connected")
			writeEvent(os.Stdout, OutgoingEvent{Type: "mqtt_connected"})

		case *messagix.Event_PublishResponse:
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
				writeEvent(os.Stdout, OutgoingEvent{
					Type: "message",
					Data: MessageEventData{
						MessageID: msg.MessageId,
						ThreadID:  msg.ThreadKey,
						SenderID:  msg.SenderId,
						Text:      msg.Text,
						Timestamp: msg.TimestampMs,
						IsUnsent:  msg.IsUnsent,
					},
				})

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
					writeEvent(os.Stdout, OutgoingEvent{
						Type: "message",
						Data: MessageEventData{
							MessageID: msg.MessageId,
							ThreadID:  threadID,
							SenderID:  msg.SenderId,
							Text:      msg.Text,
							Timestamp: msg.TimestampMs,
							IsUnsent:  msg.IsUnsent,
						},
					})
				}
			}

		case *messagix.Event_SocketError:
			log.Error().Err(e.Err).Int("attempts", e.ConnectionAttempts).Msg("Socket error")
			writeEvent(os.Stdout, OutgoingEvent{
				Type: "socket_error",
				Data: map[string]interface{}{
					"error":    e.Err.Error(),
					"attempts": e.ConnectionAttempts,
				},
			})

		case *messagix.Event_PermanentError:
			log.Error().Err(e.Err).Msg("Permanent error")
			writeEvent(os.Stdout, OutgoingEvent{
				Type: "permanent_error",
				Data: map[string]interface{}{"error": e.Err.Error()},
			})

		case *messagix.Event_Reconnected:
			log.Info().Msg("MQTT reconnected")
			writeEvent(os.Stdout, OutgoingEvent{Type: "reconnected"})
		}
	})

	go func() {
		err := client.Connect(ctx)
		if err != nil && !errors.Is(err, context.Canceled) {
			log.Error().Err(err).Msg("Connection error")
		}
	}()

	sigCh := make(chan os.Signal, 1)
	signal.Notify(sigCh, syscall.SIGINT, syscall.SIGTERM)

	stdinReader := bufio.NewReader(os.Stdin)
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

			switch cmd.Type {
			case "send_message":
				var data SendMessageData
				if err := json.Unmarshal(cmd.Data, &data); err != nil {
					log.Error().Err(err).Msg("Failed to parse send_message data")
					continue
				}

				otid := time.Now().UnixMilli()
				_, err := client.ExecuteTasks(ctx, &socket.SendMessageTask{
					ThreadId:        data.ThreadID,
					Otid:            otid,
					Source:          table.MESSENGER_INBOX_IN_THREAD,
					SendType:        table.TEXT,
					Text:            data.Text,
					SyncGroup:       1,
					InitiatingSource: table.FACEBOOK_INBOX,
					SkipUrlPreviewGen: 1,
					MultiTabEnv:      0,
				})
				if err != nil {
					log.Error().Err(err).Msg("Failed to send message")
					writeEvent(os.Stdout, OutgoingEvent{
						Type: "error",
						Data: map[string]interface{}{
							"command": cmd.Type,
							"error":   err.Error(),
							"id":      cmd.ID,
						},
					})
				} else {
					writeEvent(os.Stdout, OutgoingEvent{
						Type: "sent",
						Data: map[string]interface{}{
							"id":       cmd.ID,
							"otid":     otid,
							"thread_id": data.ThreadID,
						},
					})
				}

			case "edit_message":
				var data EditMessageData
				if err := json.Unmarshal(cmd.Data, &data); err != nil {
					log.Error().Err(err).Msg("Failed to parse edit_message data")
					continue
				}
				_, err := client.ExecuteTasks(ctx, &socket.EditMessageTask{
					MessageID: data.MessageID,
					Text:      data.Text,
				})
				if err != nil {
					log.Error().Err(err).Msg("Failed to edit message")
					writeEvent(os.Stdout, OutgoingEvent{
						Type: "error",
						Data: map[string]interface{}{
							"command": cmd.Type,
							"error":   err.Error(),
							"id":      cmd.ID,
						},
					})
				} else {
					writeEvent(os.Stdout, OutgoingEvent{
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
					continue
				}
				_, err := client.ExecuteTasks(ctx, &socket.DeleteMessageTask{
					MessageId: data.MessageID,
				})
				if err != nil {
					log.Error().Err(err).Msg("Failed to delete message")
					writeEvent(os.Stdout, OutgoingEvent{
						Type: "error",
						Data: map[string]interface{}{
							"command": cmd.Type,
							"error":   err.Error(),
							"id":      cmd.ID,
						},
					})
				} else {
					writeEvent(os.Stdout, OutgoingEvent{
						Type: "deleted",
						Data: map[string]interface{}{
							"id":         cmd.ID,
							"message_id": data.MessageID,
						},
					})
				}

			case "get_uid":
				currentUser, err := client.GetCurrentAccount()
				if err != nil {
					log.Error().Err(err).Msg("Failed to get current account")
					continue
				}
				writeEvent(os.Stdout, OutgoingEvent{
					Type: "uid",
					Data: map[string]interface{}{
						"id":   cmd.ID,
						"uid":  currentUser.GetFBID(),
						"name": currentUser.GetName(),
					},
				})

			case "stop":
				log.Info().Msg("Stop requested")
				cancel()
				return

			default:
				log.Warn().Str("type", cmd.Type).Msg("Unknown command type")
			}
		}
	}()

	select {
	case <-sigCh:
		log.Info().Msg("Signal received, shutting down")
	case <-stdinDone:
		log.Info().Msg("Stdin closed, shutting down")
	case <-ctx.Done():
		log.Info().Msg("Context cancelled, shutting down")
	}

	cancel()
	client.Disconnect()
	time.Sleep(500 * time.Millisecond)
}
