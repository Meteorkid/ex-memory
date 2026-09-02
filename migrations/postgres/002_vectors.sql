-- 002_vectors.sql: pgvector 向量存储
--
-- 换掉 ChromaDB 的真实理由不是性能，是**本地盘**：Chroma 的 persist 目录
-- 在每个副本各自的磁盘上，多副本下根本共享不了。向量进 Postgres 之后，
-- 副本无状态才真正成立。
--
-- 顺带解决一致性：向量与镜像元数据同库，账号注销时在同一个事务里删掉，
-- 不会留下孤儿向量（那是合规事故而不只是脏数据）。

CREATE EXTENSION IF NOT EXISTS vector;

CREATE TABLE IF NOT EXISTS memory_vectors (
    id TEXT PRIMARY KEY,
    exe_key TEXT NOT NULL,           -- 镜像标识，等价于 Chroma 的 collection
    embedding vector(1024) NOT NULL, -- bge-m3 维度；换模型需同步改并重建
    document TEXT NOT NULL,          -- 用于向量化的文本
    display_text TEXT,               -- 展示给模型的原话
    metadata JSONB NOT NULL DEFAULT '{}'::jsonb,
    created_at TEXT DEFAULT to_char(now() at time zone 'utc', 'YYYY-MM-DD HH24:MI:SS')
);

-- 检索永远带 exe_key 过滤，放首列
CREATE INDEX IF NOT EXISTS idx_memory_vectors_exe ON memory_vectors(exe_key);

-- dominant_speaker 过滤是生产检索路径的必经条件
CREATE INDEX IF NOT EXISTS idx_memory_vectors_speaker
    ON memory_vectors(exe_key, (metadata->>'dominant_speaker'));

-- 余弦距离索引。lists 取值按数据量调整，当前规模下 100 足够。
CREATE INDEX IF NOT EXISTS idx_memory_vectors_embedding
    ON memory_vectors USING ivfflat (embedding vector_cosine_ops) WITH (lists = 100);
