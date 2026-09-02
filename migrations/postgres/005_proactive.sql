-- 005_proactive.sql: 主动发起对话（Postgres）

CREATE TABLE IF NOT EXISTS proactive_messages (
    id SERIAL PRIMARY KEY,
    exe_key TEXT NOT NULL,
    user_id INTEGER NOT NULL,
    slug TEXT NOT NULL,
    trigger TEXT NOT NULL,
    content TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'pending',
    created_at TEXT DEFAULT to_char(now() at time zone 'utc', 'YYYY-MM-DD HH24:MI:SS'),
    delivered_at TEXT
);
CREATE INDEX IF NOT EXISTS idx_proactive_pending
    ON proactive_messages(user_id, status, created_at);

CREATE TABLE IF NOT EXISTS proactive_configs (
    exe_key TEXT PRIMARY KEY,
    enabled INTEGER NOT NULL DEFAULT 0,
    max_per_day INTEGER NOT NULL DEFAULT 2,
    quiet_start INTEGER NOT NULL DEFAULT 23,
    quiet_end INTEGER NOT NULL DEFAULT 8,
    updated_at TEXT DEFAULT to_char(now() at time zone 'utc', 'YYYY-MM-DD HH24:MI:SS')
);
