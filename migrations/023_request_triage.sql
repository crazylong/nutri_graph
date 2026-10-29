-- 023_request_triage.sql —— 未收录登记的归类结论与动作审计
--
-- triage 给列表直接显示最近一次反查，不必每次打开都打图。
-- kg_request_action 记下「挂到了哪个 ING_、谁驳的」，reject_reason 只表达否。

BEGIN;

ALTER TABLE kg_ingredient_request
    ADD COLUMN IF NOT EXISTS triage          TEXT NOT NULL DEFAULT '',
    ADD COLUMN IF NOT EXISTS triage_hit_id   TEXT NOT NULL DEFAULT '',
    ADD COLUMN IF NOT EXISTS triage_at       TIMESTAMPTZ,
    ADD COLUMN IF NOT EXISTS handled_by      TEXT NOT NULL DEFAULT '',
    ADD COLUMN IF NOT EXISTS handled_at      TIMESTAMPTZ;

ALTER TABLE kg_ingredient_request
    DROP CONSTRAINT IF EXISTS kg_ingredient_request_triage_chk;
ALTER TABLE kg_ingredient_request
    ADD CONSTRAINT kg_ingredient_request_triage_chk
    CHECK (triage IN ('', 'exists', 'likely_exists', 'not_found', 'not_food'));

CREATE TABLE IF NOT EXISTS kg_request_action (
    id          BIGSERIAL PRIMARY KEY,
    request_id  BIGINT      NOT NULL,
    action      TEXT        NOT NULL,
    payload     JSONB       NOT NULL DEFAULT '{}',
    actor       TEXT        NOT NULL DEFAULT '',
    created_at  TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS idx_req_action_req ON kg_request_action (request_id, created_at DESC);

COMMIT;
