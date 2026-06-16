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
