-- 013_import_bundle.sql —— 导入并入审核体系，取消「演示库」
--
-- 背景：012 当初把导入结果写进独立的 demo_food，理由是「公开入口不能直连审核队列」。
-- 现在整站都是演示环境，这个隔离没有意义了：导入的数据和抓取的数据应该走
-- **同一条** bundle → 审核 → kg.merge → 主图的链路，否则：
--   * 导入页看得到、问答看不到，两套数据永远对不上；
--   * 「导入的数据没有证据、没有批次、不能回滚」——正好是审核链路要解决的问题。
--
-- 三件事：
--   1. 批次表改名，去掉 demo_ 前缀（它们本来就是导入批次，不是演示专用）
--   2. 删掉 demo_food（数据表只有一张：主图；Postgres 里只留「待审核的产物」）
--   3. bundle 补两个字段，让导入来源能正确表达「别名」和「证据类型」

BEGIN;

-- ---------- 1. 批次表改名 ----------
DO $$
BEGIN
    IF to_regclass('public.demo_import_job') IS NOT NULL
       AND to_regclass('public.kg_import_job') IS NULL THEN
        ALTER TABLE demo_import_job RENAME TO kg_import_job;
    END IF;
    IF to_regclass('public.demo_import_row') IS NOT NULL
       AND to_regclass('public.kg_import_row') IS NULL THEN
        ALTER TABLE demo_import_row RENAME TO kg_import_row;
    END IF;
END $$;

-- ---------- 2. 下线演示库 ----------
-- 导入结果不再另存一份，全部进 kg_food_bundle 等审核。
DROP TABLE IF EXISTS demo_food;

-- ---------- 3. bundle 补字段 ----------
-- 别名：Excel 有「别名」列，USDA 没有。原来 approve_bundle 只从
-- [名称, food_key, 英文名] 拼别名，导入的「南美藜 / 昆诺阿藜」会整列丢掉。
ALTER TABLE kg_food_bundle ADD COLUMN IF NOT EXISTS aliases TEXT[] NOT NULL DEFAULT '{}';

-- 证据类型：原先 approve_bundle 写死 'measured'，抓取和导入都是它。
-- 表里存一份，审核时透传，将来接传统经验类数据不用改代码。
ALTER TABLE kg_food_bundle
    ADD COLUMN IF NOT EXISTS evidence_type TEXT NOT NULL DEFAULT 'measured';

-- ---------- 4. 同一食材只留一个「未结」的导入 bundle ----------
-- 重复上传同一份表不应该堆出第二份待审产物，否则审核页会被同一食材刷屏。
-- 只约束导入来源：抓取侧 kg/fetch.py 是「先删后插」，
-- 若把 crawl 也纳进来，会和在审的人工 bundle 互相卡住。
CREATE UNIQUE INDEX IF NOT EXISTS uq_open_import_bundle
    ON kg_food_bundle (food_key)
    WHERE source_key = 'excel_import'
      AND status IN ('draft', 'pending_review', 'approved');

COMMIT;
