-- 024_food_admin.sql —— 食物重录标记与后台删除审计
--
-- re_record：批准后合并时允许改名称/分类/摘要，并替换该食材的营养边。
-- provenance 不靠这一列放开，写图时单独保住。
-- kg_food_admin_action：删除和提交重录留 who/what/when，kg_request_action 只管未收录请求。

BEGIN;

ALTER TABLE kg_food_bundle
    ADD COLUMN IF NOT EXISTS re_record BOOLEAN NOT NULL DEFAULT false;

ALTER TABLE kg_node_candidate
    ADD COLUMN IF NOT EXISTS re_record BOOLEAN NOT NULL DEFAULT false;

-- 重录是独立来源：不占用公开手工录入的 IP 限额，也不和表格导入抢同一条未结 bundle。
ALTER TABLE kg_import_job
    DROP CONSTRAINT IF EXISTS kg_import_job_source_key_chk;
ALTER TABLE kg_import_job
    ADD CONSTRAINT kg_import_job_source_key_chk
    CHECK (source_key IN ('excel_import', 'manual_entry', 'fcd_crawler', 'admin_rerecord'));

DROP INDEX IF EXISTS uq_open_import_bundle;
CREATE UNIQUE INDEX IF NOT EXISTS uq_open_import_bundle
    ON kg_food_bundle (food_key, source_key)
    WHERE source_key IN ('excel_import', 'manual_entry', 'fcd_crawler', 'admin_rerecord')
      AND status IN ('draft', 'pending_review', 'approved');

CREATE TABLE IF NOT EXISTS kg_food_admin_action (
    id           BIGSERIAL PRIMARY KEY,
    actor        TEXT        NOT NULL DEFAULT '',
    action       TEXT        NOT NULL,
    canonical_id TEXT        NOT NULL DEFAULT '',
    food_name    TEXT        NOT NULL DEFAULT '',
    edge_count   INT         NOT NULL DEFAULT 0,
    detail       JSONB       NOT NULL DEFAULT '{}',
    created_at   TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS idx_food_admin_action_cid
    ON kg_food_admin_action (canonical_id, created_at DESC);

COMMIT;
