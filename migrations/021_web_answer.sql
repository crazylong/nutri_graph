-- 021_web_answer.sql —— 未收录食材的当轮网页参考
--
-- 只存临时答案和来源，不是证据，不参与图谱检索。
-- 命中审计不另建表：kg.sensitive.log_hits(scene="web") 写入已有的
-- sensitive_hit_log。该表 scene 是没有 CHECK 的 TEXT，注释枚举留在
-- 018 里不改（已应用的迁移不能改文件）。这里补一句：scene 还可以是 web。

CREATE TABLE IF NOT EXISTS web_answer_cache (
    query_hash   TEXT PRIMARY KEY,
    question     TEXT        NOT NULL,
    answer       TEXT        NOT NULL DEFAULT '',
    sources      JSONB       NOT NULL DEFAULT '[]',
    provider     TEXT        NOT NULL DEFAULT '',
    hit_count    INTEGER     NOT NULL DEFAULT 0,
    created_at   TIMESTAMPTZ NOT NULL DEFAULT now(),
    expires_at   TIMESTAMPTZ NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_web_cache_expire ON web_answer_cache (expires_at);

CREATE TABLE IF NOT EXISTS web_search_log (
    id           BIGSERIAL PRIMARY KEY,
    user_id      TEXT        NOT NULL DEFAULT '',
    query_hash   TEXT        NOT NULL,
    provider     TEXT        NOT NULL DEFAULT '',
    ok           BOOLEAN     NOT NULL DEFAULT false,
    result_count INTEGER     NOT NULL DEFAULT 0,
    kept_count   INTEGER     NOT NULL DEFAULT 0,
    blocked      TEXT        NOT NULL DEFAULT '',
    error        TEXT        NOT NULL DEFAULT '',
    elapsed_ms   INTEGER     NOT NULL DEFAULT 0,
    created_at   TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS idx_web_log_day ON web_search_log (created_at);
