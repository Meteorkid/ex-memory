-- MCP 访问令牌：长时效、可单独吊销，与服务端的 2 小时会话令牌分开存放。
-- MCP Server 以 stdio 方式跑在用户本机，没有 HTTP 请求对象可携带会话令牌，
-- 复用 tokens 表意味着每 2 小时要重新登录一次，实际上逼着用户把密码写进环境变量。
-- 单独一张表的代价是多一处存储，换来的是「一个客户端一枚令牌、丢了只吊销一枚」。
--
-- 只存 SHA-256 哈希，明文仅在签发时展示一次。
CREATE TABLE IF NOT EXISTS mcp_tokens (
    token TEXT PRIMARY KEY,
    user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    label TEXT NOT NULL DEFAULT '',
    prefix TEXT NOT NULL DEFAULT '',
    created_at TEXT NOT NULL DEFAULT (datetime('now')),
    last_used_at TEXT,
    expires_at TEXT,
    revoked_at TEXT
);

CREATE INDEX IF NOT EXISTS idx_mcp_tokens_user ON mcp_tokens(user_id);
CREATE INDEX IF NOT EXISTS idx_mcp_tokens_prefix ON mcp_tokens(prefix);
