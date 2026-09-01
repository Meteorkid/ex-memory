-- 006_tasks.sql: 异步任务表
--
-- 导入、反思、朋友圈生成、备份原本都在 HTTP 请求内同步执行，长任务会打爆
-- 网关超时；导入更是把整个 worker 的事件循环冻住（D-04）。改为提交任务后
-- 立即返回 task_id，由后台 worker 执行。
--
-- 任务状态落库而非放内存：进程重启后要能看到「那个导入到底成没成」，
-- 也要能把中断的任务捡回来重试。

CREATE TABLE IF NOT EXISTS tasks (
    id TEXT PRIMARY KEY,
    user_id INTEGER NOT NULL,
    slug TEXT,
    task_type TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'queued',   -- queued/running/succeeded/failed/cancelled
    progress INTEGER NOT NULL DEFAULT 0,      -- 0-100
    detail TEXT,                              -- 进度说明，展示给用户
    payload TEXT,                             -- JSON，任务入参
    result TEXT,                              -- JSON，成功结果
    error TEXT,                               -- 失败原因（面向用户的措辞）
    attempts INTEGER NOT NULL DEFAULT 0,
    created_at TEXT DEFAULT (datetime('now')),
    started_at TEXT,
    finished_at TEXT,
    heartbeat_at TEXT,                        -- 用于识别进程崩溃遗留的僵尸任务
    FOREIGN KEY (user_id) REFERENCES users(id)
);

-- 账户维度查询是主路径：所有业务表都以 user_id 为首列建索引
CREATE INDEX IF NOT EXISTS idx_tasks_user ON tasks(user_id, created_at DESC);
CREATE INDEX IF NOT EXISTS idx_tasks_status ON tasks(status, created_at);
