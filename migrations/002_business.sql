-- 002_business.sql —— 业务表（原 scripts/init_pg.py 的 DDL 收编）
--
-- 为什么单独一个迁移文件：业务表全是「与向量无关」的普通表，
-- 放这里能保证在任何 PostgreSQL 16 上都一次建成功；
-- 依赖 vector 扩展的 memory_fact 单独放 003，失败不牵连这一批。

BEGIN;

-- pg_trgm 供 dict_alias 的模糊匹配（trusted 扩展，库 owner 可自建）
CREATE EXTENSION IF NOT EXISTS pg_trgm;

-- ---------- 归一：别名表 ----------
CREATE TABLE IF NOT EXISTS dict_alias (
    entity_type   TEXT NOT NULL,
    canonical_id  TEXT NOT NULL,
    alias         TEXT NOT NULL,
    alias_norm    TEXT NOT NULL,
    source        TEXT DEFAULT '',
    confidence    FLOAT DEFAULT 1.0,
    created_at    TIMESTAMPTZ DEFAULT now(),
    PRIMARY KEY (entity_type, alias_norm, canonical_id)
);
CREATE INDEX IF NOT EXISTS idx_dict_alias_cid ON dict_alias (canonical_id);
CREATE INDEX IF NOT EXISTS idx_dict_alias_trgm
    ON dict_alias USING gin (alias_norm gin_trgm_ops);

-- ---------- 归一：别名回流候选表 ----------
-- 「未收录」里属于「别名漏收」的那一支会写这里（不是新食材，不要建节点）
CREATE TABLE IF NOT EXISTS dict_alias_candidate (
    id                     BIGSERIAL PRIMARY KEY,
    entity_type            TEXT NOT NULL,
    alias                  TEXT NOT NULL,
    alias_norm             TEXT NOT NULL,
    suggested_canonical_id TEXT,
    status                 TEXT DEFAULT 'pending',  -- pending | approved | rejected
    created_at             TIMESTAMPTZ DEFAULT now(),
    UNIQUE (entity_type, alias_norm)
);

-- ---------- 构建：候选关系表（LLM 抽取结果先落这里，人工确认后 MERGE 主图）----------
CREATE TABLE IF NOT EXISTS kg_candidate (
    id             BIGSERIAL PRIMARY KEY,
    batch_id       TEXT NOT NULL,
    src_type       TEXT NOT NULL,
    src_id         TEXT NOT NULL,
    rel            TEXT NOT NULL,
    dst_type       TEXT NOT NULL,
    dst_id         TEXT NOT NULL,
    dst_name       TEXT DEFAULT '',
    claim_text     TEXT DEFAULT '',
    evidence_type  TEXT DEFAULT '',
    evidence_level TEXT DEFAULT '',
    sources        JSONB DEFAULT '[]',
    confidence     FLOAT DEFAULT 0.0,
    source_locator TEXT DEFAULT '',
    status         TEXT DEFAULT 'pending',  -- pending | approved | rejected | merged
    reviewer       TEXT DEFAULT '',
    reviewed_at    TIMESTAMPTZ,
    created_at     TIMESTAMPTZ DEFAULT now(),
    UNIQUE (batch_id, src_id, rel, dst_id)
);
CREATE INDEX IF NOT EXISTS idx_kg_candidate_status ON kg_candidate (status);

-- ---------- 记忆：会话 / 消息 ----------
CREATE TABLE IF NOT EXISTS session (
    id          UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    user_id     TEXT NOT NULL,
    started_at  TIMESTAMPTZ DEFAULT now(),
    ended_at    TIMESTAMPTZ,
    summary     TEXT DEFAULT ''
);
CREATE TABLE IF NOT EXISTS message (
    id          BIGSERIAL PRIMARY KEY,
    session_id  UUID NOT NULL REFERENCES session(id),
    role        TEXT NOT NULL,
    content     TEXT NOT NULL,
    meta        JSONB DEFAULT '{}',
    tokens      INT DEFAULT 0,
    created_at  TIMESTAMPTZ DEFAULT now()
);
CREATE INDEX IF NOT EXISTS idx_message_session ON message (session_id, created_at);

-- ---------- 记忆：用户画像（硬约束，进 Cypher 过滤）----------
CREATE TABLE IF NOT EXISTS user_profile (
    user_id     TEXT PRIMARY KEY,
    allergies   TEXT[] DEFAULT '{}',
    conditions  TEXT[] DEFAULT '{}',
    diet_style  TEXT DEFAULT '',
    avoid_list  TEXT[] DEFAULT '{}',
    updated_at  TIMESTAMPTZ DEFAULT now()
);

COMMIT;
