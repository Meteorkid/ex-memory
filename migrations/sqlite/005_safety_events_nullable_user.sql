-- 005_safety_events_nullable_user.sql: 允许安全事件与账号解绑
--
-- 账号注销时，safety_events 不整行删除而是抹掉 user_id 与片段：危机与违规
-- 事件的聚合统计有留存价值，去掉关联后它不再是个人信息。原 schema 把
-- user_id 声明为 NOT NULL，与这个语义冲突。
--
-- SQLite 不支持 ALTER COLUMN 去掉 NOT NULL，只能重建表。

CREATE TABLE safety_events_new (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id INTEGER,                     -- 可空：账号注销后置空表示已解绑
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
    created_at TEXT DEFAULT (datetime('now')),
    FOREIGN KEY (user_id) REFERENCES users(id)
);

INSERT INTO safety_events_new (
    id, user_id, slug, event_type, severity, confidence, detector,
    input_hash, excerpt, action_taken, review_status, reviewed_by,
    reviewed_at, review_note, created_at
)
SELECT id, user_id, slug, event_type, severity, confidence, detector,
       input_hash, excerpt, action_taken, review_status, reviewed_by,
       reviewed_at, review_note, created_at
FROM safety_events;

DROP TABLE safety_events;
ALTER TABLE safety_events_new RENAME TO safety_events;

CREATE INDEX IF NOT EXISTS idx_safety_events_user
    ON safety_events(user_id, created_at);
CREATE INDEX IF NOT EXISTS idx_safety_events_review
    ON safety_events(review_status, severity, created_at);
