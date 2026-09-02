-- 008_billing.sql: 计费主体、计量、配额与订阅
--
-- 计量单位是**对话轮次**而非字数：实测每轮固定成本约 5700 输入 tokens
-- （SKILL.md 人格档案占大头），与用户输入长短基本无关。按字数计费既不
-- 反映真实成本，也会让用户为「在吗」这样的短消息困惑于扣了多少。
--
-- accounts 是计费主体，个人用户就是 1 人一个 account。这一层现在看是多余的，
-- 但没有它，将来做团队版就得改动所有业务表的外键。

CREATE TABLE IF NOT EXISTS accounts (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    plan TEXT NOT NULL DEFAULT 'free',
    status TEXT NOT NULL DEFAULT 'active',   -- active / suspended
    created_at TEXT DEFAULT (datetime('now'))
);

ALTER TABLE users ADD COLUMN account_id INTEGER REFERENCES accounts(id);
CREATE INDEX IF NOT EXISTS idx_users_account ON users(account_id);

-- 每次 LLM 调用一条。cost_micros 用整数存百万分之一元，避免浮点累加误差。
CREATE TABLE IF NOT EXISTS usage_records (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    account_id INTEGER NOT NULL,
    user_id INTEGER,
    slug TEXT,
    kind TEXT NOT NULL DEFAULT 'chat',       -- chat / reflect / moment / summary
    provider TEXT,
    model TEXT,
    prompt_tokens INTEGER NOT NULL DEFAULT 0,
    completion_tokens INTEGER NOT NULL DEFAULT 0,
    cached_tokens INTEGER NOT NULL DEFAULT 0,
    cost_micros INTEGER NOT NULL DEFAULT 0,
    created_at TEXT DEFAULT (datetime('now'))
);
CREATE INDEX IF NOT EXISTS idx_usage_account ON usage_records(account_id, created_at);

-- 配额按计费周期（YYYY-MM）。reserved 是预扣，settled 是已结算，
-- 两阶段扣减靠这两个字段区分——否则会出现「扣了额度没生成」或反之。
CREATE TABLE IF NOT EXISTS quotas (
    account_id INTEGER NOT NULL,
    period TEXT NOT NULL,
    turns_limit INTEGER NOT NULL,
    turns_reserved INTEGER NOT NULL DEFAULT 0,
    turns_settled INTEGER NOT NULL DEFAULT 0,
    cost_micros INTEGER NOT NULL DEFAULT 0,
    updated_at TEXT DEFAULT (datetime('now')),
    PRIMARY KEY (account_id, period)
);

CREATE TABLE IF NOT EXISTS subscriptions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    account_id INTEGER NOT NULL,
    plan TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'pending',  -- pending / active / expired / refunded
    provider TEXT,                            -- wechat / alipay / manual
    payment_ref TEXT,                         -- 外部订单号，对账用
    amount_micros INTEGER NOT NULL DEFAULT 0,
    started_at TEXT,
    expires_at TEXT,
    created_at TEXT DEFAULT (datetime('now'))
);
CREATE INDEX IF NOT EXISTS idx_subscriptions_account
    ON subscriptions(account_id, status);
CREATE UNIQUE INDEX IF NOT EXISTS idx_subscriptions_payment_ref
    ON subscriptions(payment_ref) WHERE payment_ref IS NOT NULL;
