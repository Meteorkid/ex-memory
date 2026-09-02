-- 004_relationship.sql: 关系时间线与跨会话状态（Postgres）

CREATE TABLE IF NOT EXISTS relationship_timeline (
    id SERIAL PRIMARY KEY,
    exe_key TEXT NOT NULL,
    happened_at TEXT,
    event TEXT NOT NULL,
    emotion TEXT,
    source TEXT,
    source_ref TEXT,
    created_at TEXT DEFAULT to_char(now() at time zone 'utc', 'YYYY-MM-DD HH24:MI:SS')
);
CREATE INDEX IF NOT EXISTS idx_timeline_exe
    ON relationship_timeline(exe_key, happened_at);

CREATE TABLE IF NOT EXISTS exe_states (
    exe_key TEXT PRIMARY KEY,
    mood TEXT,
    recent_context TEXT,
    updated_at TEXT DEFAULT to_char(now() at time zone 'utc', 'YYYY-MM-DD HH24:MI:SS')
);
