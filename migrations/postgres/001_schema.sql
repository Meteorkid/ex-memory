-- 001_schema.sql: PostgreSQL 完整 schema
--
-- Postgres 侧没有存量部署，不需要复刻 SQLite 那 7 步增量历史，
-- 直接给一份合并后的当前结构。此后新增改动两边各加一个文件。
--
-- 时间戳沿用 TEXT 而非 timestamptz：现有代码到处在做字符串比较
-- （expires_at < _utc_now_str()），换类型会牵动一大片比较语义。
-- 迁移期先保持形状、只换引擎，换类型作为后续独立改动。
-- 写入侧统一用 to_char(now() at time zone 'utc', ...)，与 SQLite 的
-- datetime('now') 产出同一种文本，由 core/db.py 自动翻译。

CREATE TABLE IF NOT EXISTS users (
    id SERIAL PRIMARY KEY,
    username TEXT UNIQUE NOT NULL,
    password_hash TEXT NOT NULL,
    salt TEXT NOT NULL,
    role TEXT DEFAULT 'user',
    created_at TEXT DEFAULT to_char(now() at time zone 'utc', 'YYYY-MM-DD HH24:MI:SS'),
    phone TEXT,
    phone_verified_at TEXT,
    age_confirmed_at TEXT
);
CREATE UNIQUE INDEX IF NOT EXISTS idx_users_phone ON users(phone) WHERE phone IS NOT NULL;

CREATE TABLE IF NOT EXISTS tokens (
    token TEXT PRIMARY KEY,
    user_id INTEGER NOT NULL REFERENCES users(id),
    created_at TEXT DEFAULT to_char(now() at time zone 'utc', 'YYYY-MM-DD HH24:MI:SS'),
    expires_at TEXT,
    session_id TEXT
);
CREATE INDEX IF NOT EXISTS idx_tokens_session ON tokens(session_id);
CREATE INDEX IF NOT EXISTS idx_tokens_user ON tokens(user_id);

CREATE TABLE IF NOT EXISTS refresh_tokens (
    token TEXT PRIMARY KEY,
    user_id INTEGER NOT NULL REFERENCES users(id),
    session_id TEXT NOT NULL,
    created_at TEXT DEFAULT to_char(now() at time zone 'utc', 'YYYY-MM-DD HH24:MI:SS'),
    expires_at TEXT NOT NULL,
    revoked_at TEXT,
    rotated_to TEXT,
    user_agent TEXT,
    ip TEXT
);
CREATE INDEX IF NOT EXISTS idx_refresh_user ON refresh_tokens(user_id, revoked_at);
CREATE INDEX IF NOT EXISTS idx_refresh_session ON refresh_tokens(session_id);

CREATE TABLE IF NOT EXISTS external_identities (
    provider TEXT NOT NULL,
    external_user_id TEXT NOT NULL,
    user_id INTEGER NOT NULL REFERENCES users(id),
    created_at TEXT DEFAULT to_char(now() at time zone 'utc', 'YYYY-MM-DD HH24:MI:SS'),
    PRIMARY KEY (provider, external_user_id),
    UNIQUE (provider, user_id)
);

CREATE TABLE IF NOT EXISTS consents (
    id SERIAL PRIMARY KEY,
    user_id INTEGER NOT NULL REFERENCES users(id),
    policy_type TEXT NOT NULL,
    policy_version TEXT NOT NULL,
    granted_at TEXT DEFAULT to_char(now() at time zone 'utc', 'YYYY-MM-DD HH24:MI:SS'),
    ip TEXT,
    user_agent TEXT
);
CREATE INDEX IF NOT EXISTS idx_consents_user ON consents(user_id, policy_type);

CREATE TABLE IF NOT EXISTS safety_events (
    id SERIAL PRIMARY KEY,
    user_id INTEGER REFERENCES users(id),
    slug TEXT,
    event_type TEXT NOT NULL,
    severity TEXT NOT NULL,
    confidence REAL,
    detector TEXT,
    input_hash TEXT,
    excerpt TEXT,
    action_taken TEXT NOT NULL,
    review_status TEXT DEFAULT 'pending',
    reviewed_by TEXT,
    reviewed_at TEXT,
    review_note TEXT,
    created_at TEXT DEFAULT to_char(now() at time zone 'utc', 'YYYY-MM-DD HH24:MI:SS')
);
CREATE INDEX IF NOT EXISTS idx_safety_events_user ON safety_events(user_id, created_at);
CREATE INDEX IF NOT EXISTS idx_safety_events_review
    ON safety_events(review_status, severity, created_at);

CREATE TABLE IF NOT EXISTS subject_requests (
    id SERIAL PRIMARY KEY,
    claim_type TEXT NOT NULL,
    contact TEXT NOT NULL,
    target_slug TEXT,
    target_hint TEXT,
    detail TEXT,
    identity_evidence TEXT,
    status TEXT DEFAULT 'received',
    handled_by TEXT,
    resolved_at TEXT,
    resolution_note TEXT,
    created_at TEXT DEFAULT to_char(now() at time zone 'utc', 'YYYY-MM-DD HH24:MI:SS')
);
CREATE INDEX IF NOT EXISTS idx_subject_requests_status
    ON subject_requests(status, created_at);

CREATE TABLE IF NOT EXISTS user_activity (
    user_id INTEGER NOT NULL REFERENCES users(id),
    activity_date TEXT NOT NULL,
    active_seconds INTEGER DEFAULT 0,
    last_active_at TEXT,
    cooldown_until TEXT,
    PRIMARY KEY (user_id, activity_date)
);

CREATE TABLE IF NOT EXISTS tasks (
    id TEXT PRIMARY KEY,
    user_id INTEGER NOT NULL REFERENCES users(id),
    slug TEXT,
    task_type TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'queued',
    progress INTEGER NOT NULL DEFAULT 0,
    detail TEXT,
    payload TEXT,
    result TEXT,
    error TEXT,
    attempts INTEGER NOT NULL DEFAULT 0,
    created_at TEXT DEFAULT to_char(now() at time zone 'utc', 'YYYY-MM-DD HH24:MI:SS'),
    started_at TEXT,
    finished_at TEXT,
    heartbeat_at TEXT
);
CREATE INDEX IF NOT EXISTS idx_tasks_user ON tasks(user_id, created_at DESC);
CREATE INDEX IF NOT EXISTS idx_tasks_status ON tasks(status, created_at);
