package db

import (
	"database/sql"
	"embed"
	"fmt"
	"log"
	"time"

	"github.com/pressly/goose/v3"
	_ "github.com/tursodatabase/go-libsql"
)

type LogEntry struct {
	ID             string     `json:"id"`
	ConnectedAt    time.Time  `json:"connected_at"`
	DisconnectedAt *time.Time `json:"disconnected_at,omitempty"`
}

//go:embed migrations/*.sql
var migrations embed.FS

var DB *sql.DB

func Connect(url string) error {
	var err error
	DB, err = sql.Open("libsql", url)
	if err != nil {
		return fmt.Errorf("failed to open database: %w", err)
	}

	goose.SetBaseFS(migrations)
	if err := goose.SetDialect("sqlite3"); err != nil {
		return fmt.Errorf("failed to set goose dialect: %w", err)
	}
	if err := goose.Up(DB, "migrations"); err != nil {
		return fmt.Errorf("failed to run migrations: %w", err)
	}

	log.Println("Database connected successfully")
	return nil
}

func Close() {
	if DB != nil {
		DB.Close()
	}
}

func LogSlaveConnected(id string) error {
	if DB == nil {
		return nil
	}
	_, err := DB.Exec("INSERT INTO slave_logs (id, connected_at) VALUES (?, CURRENT_TIMESTAMP)", id)
	if err != nil {
		return fmt.Errorf("failed to log: %w", err)
	}
	return nil
}

func LogSlaveDisconnected(id string) error {
	if DB == nil {
		return nil
	}
	_, err := DB.Exec("UPDATE slave_logs SET disconnected_at = CURRENT_TIMESTAMP WHERE id = ? AND disconnected_at IS NULL", id)
	if err != nil {
		return fmt.Errorf("failed to log: %w", err)
	}
	return nil
}

func GetLogs() ([]LogEntry, error) {
	if DB == nil {
		return nil, fmt.Errorf("database not connected")
	}
	rows, err := DB.Query("SELECT id, connected_at, disconnected_at FROM slave_logs ORDER BY connected_at DESC LIMIT 100")
	if err != nil {
		return nil, err
	}
	defer rows.Close()

	logs := make([]LogEntry, 0)
	for rows.Next() {
		var entry LogEntry
		var connectedAt, disconnectedAt *string
		if err := rows.Scan(&entry.ID, &connectedAt, &disconnectedAt); err != nil {
			continue
		}
		if connectedAt != nil {
			entry.ConnectedAt, _ = time.Parse(time.RFC3339, *connectedAt)
		}
		if disconnectedAt != nil {
			ts, _ := time.Parse(time.RFC3339, *disconnectedAt)
			entry.DisconnectedAt = &ts
		}
		logs = append(logs, entry)
	}
	return logs, nil
}
