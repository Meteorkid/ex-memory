-- 009_relationship.sql: 关系时间线与跨会话状态
--
-- 现有三层记忆（向量库 / 会话摘要 / 人格）都是**空间维度**的：ta 说过什么、
-- ta 是什么样的人。缺的是时间维度——「去年这时候我们还在…」需要结构化的
-- 共同经历，而不是从一堆摘要里现找。
--
-- exe_key 用 owner/slug 组合，与向量库的 collection 口径一致。

CREATE TABLE IF NOT EXISTS relationship_timeline (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    exe_key TEXT NOT NULL,
    happened_at TEXT,                    -- 事件发生时间，可能只精确到月
    event TEXT NOT NULL,
    emotion TEXT,                        -- 当时的情绪基调
    source TEXT,                         -- corpus / session / manual
    source_ref TEXT,
    created_at TEXT DEFAULT (datetime('now'))
);
CREATE INDEX IF NOT EXISTS idx_timeline_exe
    ON relationship_timeline(exe_key, happened_at);

-- 跨会话状态：ta 今天心情如何、最近在忙什么。
-- 没有它，每次新会话 ta 都从同一个初始状态开始——那不像个活着的人。
CREATE TABLE IF NOT EXISTS exe_states (
    exe_key TEXT PRIMARY KEY,
    mood TEXT,
    recent_context TEXT,
    updated_at TEXT DEFAULT (datetime('now'))
);
