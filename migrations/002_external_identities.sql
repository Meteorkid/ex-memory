-- 002_external_identities.sql: 外部身份映射到现有整数用户主键

CREATE TABLE IF NOT EXISTS external_identities (
    provider TEXT NOT NULL,
    external_user_id TEXT NOT NULL,
    user_id INTEGER NOT NULL,
    created_at TEXT DEFAULT (datetime('now')),
    PRIMARY KEY (provider, external_user_id),
    UNIQUE (provider, user_id),
    FOREIGN KEY (user_id) REFERENCES users(id)
);

CREATE INDEX IF NOT EXISTS idx_external_identities_user_id
    ON external_identities(user_id);
