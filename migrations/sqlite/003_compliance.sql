-- 003_compliance.sql: 合规基础表（同意留痕、安全事件、数据主体请求）
-- 对应 ENTERPRISE_PRD.md M0：FR-012 / FR-015 / FR-017 / FR-018

-- 版本化同意记录：出事时要能举证用户在什么时候同意了哪一版协议
CREATE TABLE IF NOT EXISTS consents (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id INTEGER NOT NULL,
    policy_type TEXT NOT NULL,           -- terms / privacy / third_party_data / emotion_analysis
    policy_version TEXT NOT NULL,
    granted_at TEXT DEFAULT (datetime('now')),
    ip TEXT,
    user_agent TEXT,
    FOREIGN KEY (user_id) REFERENCES users(id)
);

CREATE INDEX IF NOT EXISTS idx_consents_user
    ON consents(user_id, policy_type, granted_at);

-- 内容安全与危机事件。
-- excerpt 存脱敏并截断后的片段：复核者要看得懂内容才能分辨真实求救与误报，
-- 全哈希会让复核队列失去意义。注意短消息的片段就等于整条（已脱敏）消息，
-- 所以这张表必须按敏感数据对待，访问要受权限控制。
-- input_hash 用于去重与关联，不可逆推原文。
CREATE TABLE IF NOT EXISTS safety_events (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id INTEGER NOT NULL,
    slug TEXT,
    event_type TEXT NOT NULL,            -- crisis / content_input / content_output
    severity TEXT NOT NULL,              -- high / medium / low
    confidence REAL,
    detector TEXT,                       -- keyword / semantic / provider 名
    input_hash TEXT,
    excerpt TEXT,
    action_taken TEXT NOT NULL,          -- interrupted / blocked / flagged
    review_status TEXT DEFAULT 'pending',  -- pending / reviewing / resolved / dismissed
    reviewed_by TEXT,
    reviewed_at TEXT,
    review_note TEXT,
    created_at TEXT DEFAULT (datetime('now')),
    FOREIGN KEY (user_id) REFERENCES users(id)
);

CREATE INDEX IF NOT EXISTS idx_safety_events_user
    ON safety_events(user_id, created_at);
-- 人工复核队列按「待复核 + 严重度 + 时间」取，单独建索引
CREATE INDEX IF NOT EXISTS idx_safety_events_review
    ON safety_events(review_status, severity, created_at);

-- 数据主体请求：被模拟者投诉、逝者近亲属主张。
-- 提交方通常没有本站账号，所以不关联 user_id，只留联系方式。
CREATE TABLE IF NOT EXISTS subject_requests (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    claim_type TEXT NOT NULL,            -- subject_complaint / deceased_kin / other
    contact TEXT NOT NULL,
    target_slug TEXT,
    target_hint TEXT,                    -- 提交方描述的定位线索（昵称、时间段等）
    detail TEXT,
    identity_evidence TEXT,              -- 近亲属主张的身份材料引用，不存材料本身
    status TEXT DEFAULT 'received',      -- received / verifying / actioned / rejected
    handled_by TEXT,
    resolved_at TEXT,
    resolution_note TEXT,
    created_at TEXT DEFAULT (datetime('now'))
);

CREATE INDEX IF NOT EXISTS idx_subject_requests_status
    ON subject_requests(status, created_at);
