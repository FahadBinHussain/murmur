package whatsapp

import (
	"context"
	"fmt"
	"os/exec"
	"strconv"

	"github.com/rs/zerolog"
)

type SyncManager struct {
	binary        string
	store         string
	account       string
	webhookURL    string
	webhookSecret string
	maxMessages   int
	downloadMedia bool
	logger        zerolog.Logger
	cmd           *exec.Cmd
	ctx           context.Context
}

func NewSyncManager(binary, store, account, webhookURL, webhookSecret string, maxMessages int, downloadMedia bool, logger zerolog.Logger) *SyncManager {
	return &SyncManager{
		binary:        binary,
		store:         store,
		account:       account,
		webhookURL:    webhookURL,
		webhookSecret: webhookSecret,
		maxMessages:   maxMessages,
		downloadMedia: downloadMedia,
		logger:        logger,
	}
}

func (sm *SyncManager) Start(ctx context.Context) error {
	sm.ctx = ctx
	return sm.start()
}

func (sm *SyncManager) start() error {
	sm.Stop()

	args := sm.buildArgs()
	sm.logger.Info().Strs("args", args).Msg("Starting wacli sync")

	sm.cmd = exec.CommandContext(sm.ctx, sm.binary, args...)
	sm.cmd.Stdout = nil
	stderr, _ := sm.cmd.StderrPipe()

	if err := sm.cmd.Start(); err != nil {
		return fmt.Errorf("start wacli sync: %w", err)
	}

	go func() {
		if stderr != nil {
			buf := make([]byte, 4096)
			n, _ := stderr.Read(buf)
			if n > 0 {
				sm.logger.Error().Str("stderr", string(buf[:n])).Msg("wacli sync stderr")
			}
		}
		err := sm.cmd.Wait()
		if err != nil {
			sm.logger.Error().Err(err).Msg("wacli sync exited")
		} else {
			sm.logger.Info().Msg("wacli sync exited cleanly")
		}
	}()

	return nil
}

func (sm *SyncManager) Stop() error {
	if sm.cmd != nil && sm.cmd.Process != nil {
		sm.logger.Info().Msg("Stopping wacli sync")
		return sm.cmd.Process.Kill()
	}
	return nil
}

func (sm *SyncManager) Restart() error {
	sm.logger.Info().Msg("Restarting wacli sync")
	return sm.start()
}

func (sm *SyncManager) buildArgs() []string {
	args := []string{"sync", "--follow"}

	if sm.store != "" {
		args = append(args, "--store", sm.store)
	}
	if sm.account != "" {
		args = append(args, "--account", sm.account)
	}
	if sm.webhookURL != "" {
		args = append(args, "--webhook", sm.webhookURL)
	}
	if sm.webhookSecret != "" {
		args = append(args, "--webhook-secret", sm.webhookSecret)
	}
	if sm.maxMessages > 0 {
		args = append(args, "--max-messages", strconv.Itoa(sm.maxMessages))
	}
	if sm.downloadMedia {
		args = append(args, "--download-media")
	}

	return args
}
