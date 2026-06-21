package whatsapp

import (
	"context"
	"fmt"
	"os/exec"
	"strings"

	"github.com/rs/zerolog"
)

type Sender struct {
	binary string
	store  string
	account string
	logger zerolog.Logger
}

func NewSender(binary, store, account string, logger zerolog.Logger) *Sender {
	return &Sender{
		binary:  binary,
		store:   store,
		account: account,
		logger:  logger,
	}
}

func (s *Sender) SendText(ctx context.Context, to string, text string) error {
	args := s.baseArgs()
	args = append(args, "send", "text",
		"--to", to,
		"--message", text,
		"--json",
	)

	s.logger.Info().Str("to", to).Str("text", truncate(text, 50)).Msg("WhatsApp send text")

	cmd := exec.CommandContext(ctx, s.binary, args...)
	out, err := cmd.CombinedOutput()
	if err != nil {
		s.logger.Error().Err(err).Str("output", string(out)).Msg("WhatsApp send failed")
		return fmt.Errorf("wacli send text: %w", err)
	}
	return nil
}

func (s *Sender) SendPresence(ctx context.Context, to string, presence string) error {
	args := s.baseArgs()
	args = append(args, "presence", presence, "--to", to)

	cmd := exec.CommandContext(ctx, s.binary, args...)
	out, err := cmd.CombinedOutput()
	if err != nil {
		s.logger.Warn().Err(err).Str("output", string(out)).Msg("WhatsApp presence failed")
		return fmt.Errorf("wacli presence: %w", err)
	}
	return nil
}

func (s *Sender) StartTyping(ctx context.Context, to string) {
	s.SendPresence(ctx, to, "typing")
}

func (s *Sender) StopTyping(ctx context.Context, to string) {
	s.SendPresence(ctx, to, "paused")
}

func (s *Sender) baseArgs() []string {
	args := []string{}
	if s.store != "" {
		args = append(args, "--store", s.store)
	}
	if s.account != "" {
		args = append(args, "--account", s.account)
	}
	return args
}

func NormalizeJID(jid string) string {
	jid = strings.TrimSpace(jid)
	if strings.Contains(jid, "@") {
		return jid
	}
	return jid + "@s.whatsapp.net"
}
