-- 凭据只保留带认证的密文；账号导出使用服务层白名单，不能导出此表原始行。
CREATE TABLE IF NOT EXISTS user_model_settings (
    user_id INTEGER PRIMARY KEY REFERENCES users(id) ON DELETE CASCADE,
    revision INTEGER NOT NULL DEFAULT 0,
    llm_provider TEXT NOT NULL DEFAULT 'deepseek',
    llm_base_url TEXT NOT NULL DEFAULT 'https://api.deepseek.com',
    llm_model TEXT NOT NULL DEFAULT 'deepseek-chat',
    llm_key_ciphertext TEXT,
    llm_key_last4 TEXT,
    embedding_provider TEXT NOT NULL DEFAULT 'siliconflow',
    embedding_base_url TEXT NOT NULL DEFAULT 'https://api.siliconflow.cn/v1',
    embedding_model TEXT NOT NULL DEFAULT 'BAAI/bge-m3',
    embedding_key_ciphertext TEXT,
    embedding_key_last4 TEXT,
    embedding_locked INTEGER NOT NULL DEFAULT 0 CHECK (embedding_locked IN (0, 1)),
    updated_at TEXT NOT NULL DEFAULT (datetime('now'))
);
