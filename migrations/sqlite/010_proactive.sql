-- 010_proactive.sql: 主动发起对话
--
-- 这是让 ta「活着」最关键的一项：现在完全被动，用户不说话就永远沉默。
-- 真实关系里「ta 突然发来一条消息」的情绪冲击远大于「我问 ta 答」——
-- 一个永远等你先开口的对象，本质上是个工具而不是个人。
--
-- 没有推送基础设施，所以做成「ta 在你不在的时候发过消息」：后台生成入库，
-- 用户下次打开时看到。体感上这恰恰是对的。

CREATE TABLE IF NOT EXISTS proactive_messages (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    exe_key TEXT NOT NULL,
    user_id INTEGER NOT NULL,
    slug TEXT NOT NULL,
    trigger TEXT NOT NULL,               -- morning / night / silence / anniversary
    content TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'pending',  -- pending / delivered / dismissed
    created_at TEXT DEFAULT (datetime('now')),
    delivered_at TEXT
);
CREATE INDEX IF NOT EXISTS idx_proactive_pending
    ON proactive_messages(user_id, status, created_at);

CREATE TABLE IF NOT EXISTS proactive_configs (
    exe_key TEXT PRIMARY KEY,
    enabled INTEGER NOT NULL DEFAULT 0,  -- 默认关闭：主动消息必须用户明确开启
    max_per_day INTEGER NOT NULL DEFAULT 2,
    quiet_start INTEGER NOT NULL DEFAULT 23,  -- 免打扰起始小时
    quiet_end INTEGER NOT NULL DEFAULT 8,
    updated_at TEXT DEFAULT (datetime('now'))
);
