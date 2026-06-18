package database

import (
	"context"
	"fmt"

	"github.com/jackc/pgx/v5/pgxpool"
)

type DB struct {
	pool *pgxpool.Pool
}

func New(ctx context.Context, dsn string) (*DB, error) {
	pool, err := pgxpool.New(ctx, dsn)
	if err != nil {
		return nil, fmt.Errorf("connect: %w", err)
	}
	if err := pool.Ping(ctx); err != nil {
		return nil, fmt.Errorf("ping: %w", err)
	}
	db := &DB{pool: pool}
	if err := db.migrate(ctx); err != nil {
		return nil, fmt.Errorf("migrate: %w", err)
	}
	return db, nil
}

func (db *DB) Close() {
	db.pool.Close()
}

func (db *DB) migrate(ctx context.Context) error {
	_, err := db.pool.Exec(ctx, `
		CREATE TABLE IF NOT EXISTS thread_models (
			thread_id   BIGINT PRIMARY KEY,
			chat_model  TEXT NOT NULL DEFAULT '',
			image_model TEXT NOT NULL DEFAULT '',
			updated_at  TIMESTAMPTZ NOT NULL DEFAULT NOW()
		)
	`)
	if err != nil {
		return err
	}
	_, err = db.pool.Exec(ctx, `
		CREATE TABLE IF NOT EXISTS messages (
			id SERIAL PRIMARY KEY,
			thread_id   BIGINT NOT NULL,
			role        TEXT NOT NULL,
			content     TEXT NOT NULL,
			created_at  TIMESTAMPTZ NOT NULL DEFAULT NOW()
		)
	`)
	if err != nil {
		return err
	}
	_, err = db.pool.Exec(ctx, `
		CREATE INDEX IF NOT EXISTS idx_messages_thread_id ON messages(thread_id)
	`)
	return err
}

type ThreadModel struct {
	ChatModel  string
	ImageModel string
}

func (db *DB) GetThreadModels(ctx context.Context, threadID int64) (ThreadModel, error) {
	var m ThreadModel
	err := db.pool.QueryRow(ctx,
		`SELECT chat_model, image_model FROM thread_models WHERE thread_id = $1`,
		threadID,
	).Scan(&m.ChatModel, &m.ImageModel)
	if err != nil {
		return ThreadModel{}, err
	}
	return m, nil
}

func (db *DB) SetChatModel(ctx context.Context, threadID int64, model string) error {
	_, err := db.pool.Exec(ctx, `
		INSERT INTO thread_models (thread_id, chat_model, updated_at)
		VALUES ($1, $2, NOW())
		ON CONFLICT (thread_id) DO UPDATE SET chat_model = $2, updated_at = NOW()
	`, threadID, model)
	return err
}

func (db *DB) SetImageModel(ctx context.Context, threadID int64, model string) error {
	_, err := db.pool.Exec(ctx, `
		INSERT INTO thread_models (thread_id, image_model, updated_at)
		VALUES ($1, $2, NOW())
		ON CONFLICT (thread_id) DO UPDATE SET image_model = $2, updated_at = NOW()
	`, threadID, model)
	return err
}

func (db *DB) GetAllThreadModels(ctx context.Context) (map[int64]ThreadModel, error) {
	rows, err := db.pool.Query(ctx, `SELECT thread_id, chat_model, image_model FROM thread_models`)
	if err != nil {
		return nil, err
	}
	defer rows.Close()

	result := make(map[int64]ThreadModel)
	for rows.Next() {
		var threadID int64
		var m ThreadModel
		if err := rows.Scan(&threadID, &m.ChatModel, &m.ImageModel); err != nil {
			return nil, err
		}
		result[threadID] = m
	}
	return result, rows.Err()
}

type Message struct {
	Role    string
	Content string
}

func (db *DB) SaveMessages(ctx context.Context, threadID int64, messages []Message) error {
	if len(messages) == 0 {
		return nil
	}
	tx, err := db.pool.Begin(ctx)
	if err != nil {
		return err
	}
	defer tx.Rollback(ctx)
	for _, m := range messages {
		_, err := tx.Exec(ctx,
			`INSERT INTO messages (thread_id, role, content) VALUES ($1, $2, $3)`,
			threadID, m.Role, m.Content)
		if err != nil {
			return err
		}
	}
	return tx.Commit(ctx)
}

func (db *DB) GetRecentMessages(ctx context.Context, threadID int64, limit int) ([]Message, error) {
	rows, err := db.pool.Query(ctx,
		`SELECT role, content FROM messages WHERE thread_id = $1 ORDER BY created_at DESC LIMIT $2`,
		threadID, limit)
	if err != nil {
		return nil, err
	}
	defer rows.Close()

	var messages []Message
	for rows.Next() {
		var m Message
		if err := rows.Scan(&m.Role, &m.Content); err != nil {
			return nil, err
		}
		messages = append(messages, m)
	}
	// reverse to chronological order
	for i, j := 0, len(messages)-1; i < j; i, j = i+1, j-1 {
		messages[i], messages[j] = messages[j], messages[i]
	}
	return messages, rows.Err()
}
