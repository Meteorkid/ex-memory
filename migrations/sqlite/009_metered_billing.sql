-- 009_metered_billing.sql: 按量计费账本（充值余额 / 记账流水 / 充值订单）
--
-- 商业模式从「月度套餐订阅」切为「按对话轮次直接计费」：
--   - 每轮固定单价 TURN_PRICE_MICROS（config），对话放行由「余额是否足够」决定
--   - 用户先充值进余额，每轮对话实时从余额扣减
--
-- account_balances 是余额账本（单账户一行）。
--   - reserve 用「原子减」预扣一轮单价：UPDATE ... SET balance = balance - ? WHERE balance >= ?
--     保证并发不超扣；余额不足则该 UPDATE 影响 0 行，判定为余额不足。
--   - settle 只落 ledger 流水（balance 已在 reserve 扣除），release 则把预扣加回。
-- ledger 是不可变记账流水，充值/扣费/退款都写一条，金额单位为 micros：
--   - topup  充值入账  +amount
--   - consume 对话扣费  -turn_price
--   - refund 充值退款  -amount
--   - adjust 人工调整  ±amount
-- 对账（NFR-006）的口径：Σ(topup+refund) 与 system/公网侧净入金比对，Σ(consume) 与
-- 计量账单比对，偏差即由 ledger 逐条定位。
-- topup_orders 是真实充值订单，payment_ref 唯一约束保证同一笔渠道支付不重复入账。

CREATE TABLE IF NOT EXISTS account_balances (
    account_id INTEGER PRIMARY KEY,
    balance_micros INTEGER NOT NULL DEFAULT 0,
    updated_at TEXT DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS ledger (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    account_id INTEGER NOT NULL,
    kind TEXT NOT NULL,                 -- topup / consume / refund / adjust
    amount_micros INTEGER NOT NULL,      -- 正入账 / 负扣减
    turn_price_micros INTEGER,           -- consume 时的当轮单价，便于追溯计价
    ref_type TEXT,                       -- topup_order / usage_record / manual
    ref_id TEXT,
    memo TEXT,
    created_at TEXT DEFAULT (datetime('now'))
);
CREATE INDEX IF NOT EXISTS idx_ledger_account ON ledger(account_id, id);
CREATE INDEX IF NOT EXISTS idx_ledger_kind ON ledger(kind, created_at);

CREATE TABLE IF NOT EXISTS topup_orders (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    account_id INTEGER NOT NULL,
    provider TEXT NOT NULL,             -- wechat / alipay / manual
    amount_micros INTEGER NOT NULL,
    status TEXT NOT NULL DEFAULT 'pending',  -- pending / paid / refunded / closed
    payment_ref TEXT,                   -- 外部渠道单号，对账用
    channel_trade_no TEXT,              -- 渠道交易号（回调返回）
    paid_at TEXT,
    created_at TEXT DEFAULT (datetime('now'))
);
CREATE INDEX IF NOT EXISTS idx_topup_account
    ON topup_orders(account_id, status);
CREATE UNIQUE INDEX IF NOT EXISTS idx_topup_payment_ref
    ON topup_orders(payment_ref) WHERE payment_ref IS NOT NULL;