-- +goose Up
-- +goose StatementBegin
CREATE TABLE slave_logs (
	id TEXT PRIMARY KEY,
	connected_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
	disconnected_at TIMESTAMP DEFAULT NULL
);
-- +goose StatementEnd

-- +goose Down
-- +goose StatementBegin
DROP TABLE slave_logs;
-- +goose StatementEnd
