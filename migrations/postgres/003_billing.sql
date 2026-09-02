-- 003_billing.sql: 计费主体、计量、配额与订阅（Postgres）
-- 结构与 sqlite/008_billing.sql 对齐，差异只在自增与索引语法。

CREATE TABLE IF NOT EXISTS accounts (
    id SERIAL PRIMARY KEY,
    plan TEXT NOT NULL DEFAULT 'free',
    status TEXT NOT NULL DEFAULT 'active',
    created_at TEXT DEFAULT to_char(now() at time zone 'utc', 'YYYY-MM-DD HH24:MI:SS')
);

ALTER TABLE users ADD COLUMN IF NOT EXISTS account_id INTEGER REFERENCES accounts(id);
CREATE INDEX IF NOT EXISTS idx_users_account ON users(account_id);

CREATE TABLE IF NOT EXISTS usage_records (
    id SERIAL PRIMARY KEY,
    account_id INTEGER NOT NULL,
    user_id INTEGER,
    slug TEXT,
    kind TEXT NOT NULL DEFAULT 'chat',
    provider TEXT,
    model TEXT,
    prompt_tokens INTEGER NOT NULL DEFAULT 0,
    completion_tokens INTEGER NOT NULL DEFAULT 0,
    cached_tokens INTEGER NOT NULL DEFAULT 0,
    cost_micros BIGINT NOT NULL DEFAULT 0,
    created_at TEXT DEFAULT to_char(now() at time zone 'utc', 'YYYY-MM-DD HH24:MI:SS')
);
CREATE INDEX IF NOT EXISTS idx_usage_account ON usage_records(account_id, created_at);

CREATE TABLE IF NOT EXISTS quotas (
    account_id INTEGER NOT NULL,
    period TEXT NOT NULL,
    turns_limit INTEGER NOT NULL,
    turns_reserved INTEGER NOT NULL DEFAULT 0,
    turns_settled INTEGER NOT NULL DEFAULT 0,
    cost_micros BIGINT NOT NULL DEFAULT 0,
    updated_at TEXT DEFAULT to_char(now() at time zone 'utc', 'YYYY-MM-DD HH24:MI:SS'),
    PRIMARY KEY (account_id, period)
);

CREATE TABLE IF NOT EXISTS subscriptions (
    id SERIAL PRIMARY KEY,
    account_id INTEGER NOT NULL,
    plan TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'pending',
    provider TEXT,
    payment_ref TEXT,
    amount_micros BIGINT NOT NULL DEFAULT 0,
    started_at TEXT,
    expires_at TEXT,
    created_at TEXT DEFAULT to_char(now() at time zone 'utc', 'YYYY-MM-DD HH24:MI:SS')
);
CREATE INDEX IF NOT EXISTS idx_subscriptions_account
    ON subscriptions(account_id, status);
CREATE UNIQUE INDEX IF NOT EXISTS idx_subscriptions_payment_ref
    ON subscriptions(payment_ref) WHERE payment_ref IS NOT NULL;
