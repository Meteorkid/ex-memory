-- 004_user_guard.sql: 准入与使用强度保护
-- 对应 ENTERPRISE_PRD.md M0：FR-020 / FR-022 / FR-023

-- 实名与年龄门槛。SQLite 不支持 ADD COLUMN IF NOT EXISTS，
-- 但迁移由 schema_version 保证只执行一次。
ALTER TABLE users ADD COLUMN phone TEXT;
ALTER TABLE users ADD COLUMN phone_verified_at TEXT;
ALTER TABLE users ADD COLUMN age_confirmed_at TEXT;

CREATE UNIQUE INDEX IF NOT EXISTS idx_users_phone
    ON users(phone) WHERE phone IS NOT NULL;

-- 使用强度：HealthTracker 原先把状态放在进程内字典，重启和多设备都会错乱。
-- 按「用户 + 自然日」累计，跨设备与跨进程一致。
CREATE TABLE IF NOT EXISTS user_activity (
    user_id INTEGER NOT NULL,
    activity_date TEXT NOT NULL,         -- YYYY-MM-DD，本地日
    active_seconds INTEGER DEFAULT 0,
    last_active_at TEXT,
    cooldown_until TEXT,                 -- 达到上限后的强制冷静期截止时刻
    PRIMARY KEY (user_id, activity_date),
    FOREIGN KEY (user_id) REFERENCES users(id)
);
