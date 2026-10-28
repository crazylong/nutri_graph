-- 015_manual_entry_source.sql —— 手工录入也算一个导入来源
--
-- 需求 3（按固定表格导入）做完之后，需求 2（手工录入）落成了「填一张小表单」。
-- 它和上传 Excel 是同一种数据、同一个审核队列，只是没有文件，
-- 所以给它独立的 source_key，而不是冒充 excel_import：
--
--   * 看板上能分清「这批是别人传的表」还是「一条条填的」；
--   * Excel 的重导语义是「整批覆盖」，手工录入是「单条提交」，
--     混在一个 source_key 里，将来做「撤销整批」会误伤。
--
-- 唯一索引要跟着扩：uq_open_import_bundle 是按 (food_key, source_key) 建的，
-- source_key 不在白名单里的行**不进索引**，也就等于没有「同食材只留一份未结」的保护。

BEGIN;

DROP INDEX IF EXISTS uq_open_import_bundle;

CREATE UNIQUE INDEX IF NOT EXISTS uq_open_import_bundle
    ON kg_food_bundle (food_key, source_key)
    WHERE source_key IN ('excel_import', 'manual_entry', 'fcd_crawler')
      AND status IN ('draft', 'pending_review', 'approved');

DROP INDEX IF EXISTS idx_kg_food_bundle_open_source;

CREATE INDEX IF NOT EXISTS idx_kg_food_bundle_open_source
    ON kg_food_bundle (source_key, status, created_at DESC)
    WHERE source_key IN ('excel_import', 'manual_entry', 'fcd_crawler');

ALTER TABLE kg_import_job
    DROP CONSTRAINT IF EXISTS kg_import_job_source_key_chk;

ALTER TABLE kg_import_job
    ADD CONSTRAINT kg_import_job_source_key_chk
    CHECK (source_key IN ('excel_import', 'manual_entry', 'fcd_crawler'));

COMMENT ON COLUMN kg_import_job.source_key IS
    'excel_import = 页面上传表格；manual_entry = 页面单条录入；fcd_crawler = 成分表抓取包';

COMMIT;
