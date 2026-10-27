-- 003_memory_vector.sql —— 长期记忆的向量表
--
-- 单独拆出来的原因：pgvector 的 control 文件没有 trusted = true，
-- 业务角色（即便数据库 owner）执行 CREATE EXTENSION vector 会报
--   InsufficientPrivilege: permission denied to create extension "vector"
-- 拆成独立文件后，002 能正常建完全部业务表，只有这一步需要 DBA 介入，
-- 失败也只影响 memory_fact 一张表，不会把前面 7 张表一起回滚。
--
-- 修复方式（只需执行一次，之后 IF NOT EXISTS 就是无操作）：
--   psql -U postgres -d <db> -c 'CREATE EXTENSION IF NOT EXISTS vector'

BEGIN;

CREATE EXTENSION IF NOT EXISTS vector;

CREATE TABLE IF NOT EXISTS memory_fact (
    id          BIGSERIAL PRIMARY KEY,
    user_id     TEXT NOT NULL,
    fact_type   TEXT DEFAULT '',
    content     TEXT NOT NULL,
    embedding   vector(1024),
    confidence  FLOAT DEFAULT 0.5,
    created_at  TIMESTAMPTZ DEFAULT now()
);
CREATE INDEX IF NOT EXISTS idx_memory_fact_user ON memory_fact (user_id);

COMMIT;
