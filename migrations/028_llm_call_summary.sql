-- 028_llm_call_summary.sql —— 超长调用的摘要
-- 正文仍按 TRACE_TEXT_MAX 截断；摘要只在超出时由后台补上。

BEGIN;

ALTER TABLE llm_call_log
    ADD COLUMN IF NOT EXISTS summary TEXT NOT NULL DEFAULT '';

COMMIT;
