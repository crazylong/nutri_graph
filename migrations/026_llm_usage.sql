-- 026_llm_usage.sql —— 模型账本（兼 span）和日配额
-- 不建 (ts::date) 索引：timestamptz 转 date 不是 IMMUTABLE。

BEGIN;

CREATE TABLE IF NOT EXISTS llm_call_log (
    id                BIGSERIAL PRIMARY KEY,
    ts                TIMESTAMPTZ NOT NULL DEFAULT now(),
    rid               TEXT        NOT NULL DEFAULT '',
    span_id           TEXT        NOT NULL DEFAULT '',
    parent_span_id    TEXT        NOT NULL DEFAULT '',
    kind              TEXT        NOT NULL DEFAULT 'llm',
    node              TEXT        NOT NULL DEFAULT '',
    user_id           TEXT        NOT NULL DEFAULT '',
    session_id        TEXT,
    model             TEXT        NOT NULL DEFAULT '',
    purpose           TEXT        NOT NULL DEFAULT 'unknown',
    input             TEXT        NOT NULL DEFAULT '',
    output            TEXT        NOT NULL DEFAULT '',
    prompt_tokens     INT         NOT NULL DEFAULT 0,
    completion_tokens INT         NOT NULL DEFAULT 0,
    latency_ms        INT         NOT NULL DEFAULT 0,
    ok                BOOLEAN     NOT NULL DEFAULT true
);

CREATE INDEX IF NOT EXISTS idx_llm_call_ts      ON llm_call_log (ts DESC);
CREATE INDEX IF NOT EXISTS idx_llm_call_user_ts ON llm_call_log (user_id, ts DESC);
CREATE INDEX IF NOT EXISTS idx_llm_call_rid     ON llm_call_log (rid);
CREATE INDEX IF NOT EXISTS idx_llm_call_session ON llm_call_log (session_id, ts DESC);

CREATE TABLE IF NOT EXISTS chat_usage_day (
    user_key  TEXT NOT NULL,
    day       DATE NOT NULL,
    n         INT  NOT NULL DEFAULT 0,
    PRIMARY KEY (user_key, day)
);

CREATE TABLE IF NOT EXISTS anon_issue_day (
    ip   TEXT NOT NULL,
    day  DATE NOT NULL,
    n    INT  NOT NULL DEFAULT 0,
    PRIMARY KEY (ip, day)
);

COMMIT;
