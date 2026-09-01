-- 007_refresh_tokens.sql: 刷新令牌与会话吊销
--
-- 原先只有一种 token，固定 7 天过期：期限长则被盗后窗口长，期限短则用户
-- 频繁重登。拆成短期 access + 长期 refresh 后两者兼顾，且 refresh 可被
-- 单独吊销——「登出全部设备」才有实现基础。
--
-- session_id 把同一次登录签发的 access 与 refresh 串起来，
-- 吊销一次登录时两者一起失效。

CREATE TABLE IF NOT EXISTS refresh_tokens (
    token TEXT PRIMARY KEY,
    user_id INTEGER NOT NULL,
    session_id TEXT NOT NULL,
    created_at TEXT DEFAULT (datetime('now')),
    expires_at TEXT NOT NULL,
    revoked_at TEXT,
    rotated_to TEXT,              -- 轮换后指向新令牌，用于识别重放
    user_agent TEXT,
    ip TEXT,
    FOREIGN KEY (user_id) REFERENCES users(id)
);

CREATE INDEX IF NOT EXISTS idx_refresh_user ON refresh_tokens(user_id, revoked_at);
CREATE INDEX IF NOT EXISTS idx_refresh_session ON refresh_tokens(session_id);

-- access token 关联到 session，便于按会话整体吊销
ALTER TABLE tokens ADD COLUMN session_id TEXT;
CREATE INDEX IF NOT EXISTS idx_tokens_session ON tokens(session_id);
