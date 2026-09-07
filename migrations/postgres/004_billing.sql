-- 004_billing 对应 sqlite 侧 009_metered_billing：按对话轮次直接计费账本
-- （sqlite 009 的 postgres 方言版）

CREATE TABLE IF NOT EXISTS account_balances (
    account_id INTEGER PRIMARY KEY,
    balance_micros BIGINT NOT NULL DEFAULT 0,
    updated_at TEXT DEFAULT to_char(now() at time zone 'utc', 'YYYY-MM-DD HH24:MI:SS')
);

CREATE TABLE IF NOT EXISTS ledger (
    id SERIAL PRIMARY KEY,
    account_id INTEGER NOT NULL,
    kind TEXT NOT NULL,                 -- topup / consume / refund / adjust
    amount_micros BIGINT NOT NULL,       -- 正入账 / 负扣减
    turn_price_micros INTEGER,
    ref_type TEXT,
    ref_id TEXT,
    memo TEXT,
    created_at TEXT DEFAULT to_char(now() at time zone 'utc', 'YYYY-MM-DD HH24:MI:SS')
);
CREATE INDEX IF NOT EXISTS idx_ledger_account ON ledger(account_id, id);
CREATE INDEX IF NOT EXISTS idx_ledger_kind ON ledger(kind, created_at);

CREATE TABLE IF NOT EXISTS topup_orders (
    id SERIAL PRIMARY KEY,
    account_id INTEGER NOT NULL,
    provider TEXT NOT NULL,
    amount_micros BIGINT NOT NULL,
    status TEXT NOT NULL DEFAULT 'pending',  -- pending / paid / refunded / closed
    payment_ref TEXT,
    channel_trade_no TEXT,
    paid_at TEXT,
    created_at TEXT DEFAULT to_char(now() at time zone 'utc', 'YYYY-MM-DD HH24:MI:SS')
);
CREATE INDEX IF NOT EXISTS idx_topup_account
    ON topup_orders(account_id, status);
CREATE UNIQUE INDEX IF NOT EXISTS idx_topup_payment_ref
    ON topup_orders(payment_ref) WHERE payment_ref IS NOT NULL;