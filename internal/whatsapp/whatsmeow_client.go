package whatsapp

import (
	"context"
	"fmt"

	"github.com/rs/zerolog"
	"go.mau.fi/whatsmeow"
	"go.mau.fi/whatsmeow/store/sqlstore"
	"go.mau.fi/whatsmeow/types"
	"go.mau.fi/whatsmeow/types/events"
	waLog "go.mau.fi/whatsmeow/util/log"
	waE2E "go.mau.fi/whatsmeow/proto/waE2E"
	_ "modernc.org/sqlite"
)

type WhatsmeowClient struct {
	client      *whatsmeow.Client
	deviceStore *sqlstore.Container
	logger      zerolog.Logger
	handler     MessageHandler
	connected   bool
}

func NewWhatsmeowClient(dbPath string, logger zerolog.Logger, handler MessageHandler) (*WhatsmeowClient, error) {
	log := waLog.Zerolog(logger.With().Str("component", "whatsmeow").Logger())

	container, err := sqlstore.New(context.Background(), "sqlite3", "file:"+dbPath+"?_foreign_keys=on", log)
	if err != nil {
		return nil, fmt.Errorf("create device store: %w", err)
	}

	deviceStore, err := container.GetFirstDevice(context.Background())
	if err != nil {
		return nil, fmt.Errorf("get first device: %w", err)
	}

	client := whatsmeow.NewClient(deviceStore, log)

	w := &WhatsmeowClient{
		client:      client,
		deviceStore: container,
		logger:      logger,
		handler:     handler,
	}

	client.AddEventHandler(w.handleEvent)

	return w, nil
}

func (w *WhatsmeowClient) handleEvent(evt interface{}) {
	switch e := evt.(type) {
	case *events.Message:
		w.handleMessage(e)
	case *events.Connected:
		w.connected = true
		w.logger.Info().Msg("WhatsApp connected")
	case *events.Disconnected:
		w.connected = false
		w.logger.Warn().Msg("WhatsApp disconnected")
	case *events.LoggedOut:
		w.connected = false
		w.logger.Error().Msg("WhatsApp logged out")
	case *events.QR:
		w.logger.Info().Msg("WhatsApp QR code received - scan with your phone")
	}
}

func (w *WhatsmeowClient) handleMessage(evt *events.Message) {
	if evt.Info.IsFromMe {
		return
	}

	text := evt.Message.GetConversation()
	if text == "" {
		if evt.Message.ExtendedTextMessage != nil {
			text = evt.Message.ExtendedTextMessage.GetText()
		}
	}

	if text == "" {
		return
	}

	chatJID := ChatJID{
		User:   evt.Info.Chat.User,
		Server: evt.Info.Chat.Server,
	}

	msg := ParsedMessage{
		Chat:      chatJID,
		ID:        evt.Info.ID,
		SenderJID: evt.Info.Sender.User + "@s.whatsapp.net",
		Timestamp: evt.Info.Timestamp,
		FromMe:    evt.Info.IsFromMe,
		Text:      text,
		PushName:  evt.Info.PushName,
	}

	w.logger.Info().
		Str("chat", chatJID.String()).
		Str("text", truncate(text, 50)).
		Msg("WhatsApp message received via whatsmeow")

	if w.handler != nil {
		w.handler(context.Background(), msg)
	}
}

func (w *WhatsmeowClient) Connect(ctx context.Context) error {
	if w.client.Store.ID == nil {
		qrChan, _ := w.client.GetQRChannel(ctx)
		go func() {
			for evt := range qrChan {
				if evt.Event == "code" {
					w.logger.Info().Msg("Scan QR code to link WhatsApp")
				}
			}
		}()
	}

	err := w.client.Connect()
	if err != nil {
		return fmt.Errorf("connect: %w", err)
	}

	return nil
}

func (w *WhatsmeowClient) Disconnect() {
	if w.client != nil {
		w.client.Disconnect()
	}
}

func (w *WhatsmeowClient) SendText(ctx context.Context, to string, text string) error {
	jid, err := types.ParseJID(to)
	if err != nil {
		return fmt.Errorf("parse JID: %w", err)
	}

	msg := &waE2E.Message{
		Conversation: &text,
	}

	_, err = w.client.SendMessage(ctx, jid, msg)
	if err != nil {
		return fmt.Errorf("send message: %w", err)
	}

	w.logger.Info().Str("to", to).Str("text", truncate(text, 50)).Msg("WhatsApp message sent via whatsmeow")
	return nil
}

func (w *WhatsmeowClient) IsConnected() bool {
	return w.connected
}

func (w *WhatsmeowClient) IsLoggedIn() bool {
	return w.client.Store.ID != nil
}
