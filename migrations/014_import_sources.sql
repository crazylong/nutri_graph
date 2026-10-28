-- 014_import_sources.sql —— 导入链路支持多个来源
--
-- 背景：13 号迁移把「导入」统一进了 kg_food_bundle，但隐含假设是只有一个来源
-- （excel_import）。现在要把中国食物成分表的离线抓取包（fcd_crawler）也并进来，
-- 两者走同一条 审核 → merge 链路，只是 source_key 不同。
--
-- 两件事：
--   1. 批次表记住来源，否则 /kg/import/jobs 分不清「网友上传」和「脚本导入」
--   2. 唯一索引从「按来源」改成「按来源 + 食材」
--
-- 为什么索引要带上 source_key：
--   _stage_one 的查找条件是 (food_key, source_key, status IN 未结)。
--   如果索引只约束 food_key，那么同一食材先被 Excel 导入、又被爬虫包导入时，
--   两次查找各自看不到对方的行，第二条 INSERT 会撞唯一约束 —— 要么报错，
--   要么逼业务代码去猜「另一来源的那份要不要覆盖」，两种情况都不对。
--   带上 source_key 后，索引约束和查找条件严格一致。
--
--   代价：同一食材可能在两个来源下各有一份待审 bundle。这是可接受的：
--   审核台看到两条同名的，正好可以选数据更权威的那条合并、驳回另一条。

BEGIN;

-- ---------- 1. 批次来源 ----------
ALTER TABLE kg_import_job
    ADD COLUMN IF NOT EXISTS source_key TEXT NOT NULL DEFAULT 'excel_import';

COMMENT ON COLUMN kg_import_job.source_key IS
    'excel_import = 页面公开上传；fcd_crawler = 中国食物成分表离线抓取包';

CREATE INDEX IF NOT EXISTS idx_kg_import_job_source
    ON kg_import_job (source_key, created_at DESC);

-- ---------- 2. 唯一索引：按来源 + 食材 ----------
DROP INDEX IF EXISTS uq_open_import_bundle;

CREATE UNIQUE INDEX IF NOT EXISTS uq_open_import_bundle
    ON kg_food_bundle (food_key, source_key)
    WHERE source_key IN ('excel_import', 'fcd_crawler')
      AND status IN ('draft', 'pending_review', 'approved');

-- 看板按来源过滤 + 按创建时间倒序，走这个索引
CREATE INDEX IF NOT EXISTS idx_kg_food_bundle_open_source
    ON kg_food_bundle (source_key, status, created_at DESC)
    WHERE source_key IN ('excel_import', 'fcd_crawler');

COMMIT;
