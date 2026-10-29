-- 与 SQLite 保持相同形状，兼容现有数据库适配层。
CREATE TABLE IF NOT EXISTS mcp_tokens (
    token TEXT PRIMARY KEY,
    user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    label TEXT NOT NULL DEFAULT '',
    prefix TEXT NOT NULL DEFAULT '',
    created_at TEXT NOT NULL DEFAULT to_char(now() at time zone 'utc', 'YYYY-MM-DD HH24:MI:SS'),
    last_used_at TEXT,
    expires_at TEXT,
    revoked_at TEXT
);

CREATE INDEX IF NOT EXISTS idx_mcp_tokens_user ON mcp_tokens(user_id);
CREATE INDEX IF NOT EXISTS idx_mcp_tokens_prefix ON mcp_tokens(prefix);
