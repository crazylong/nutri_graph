-- 012_demo_import.sql
--
-- 公开演示用的「Excel 导入食物」三张表。
--
-- 为什么单独建表，而不是复用 kg_food_bundle：
--   导入页对所有人开放（不登录）。kg_food_bundle 是**审核队列**，
--   后面接着 candidate → merge → 主图，是唯一写入口的前置闸门。
--   如果公开接口直接往那里灌，任何人都能几十秒产出上百条待审记录，
--   把运营的审核列表刷成噪声，还要占用 merge_pending 的合并开销。
--   演示数据只进 demo_food，随时可清空，主图零风险。
--
-- 契约版本：模板 v1（21 列宽表，一行一个食物），见 docs/plan/admin_plan_v2.md §6.1

BEGIN;

-- ============================================================
-- 导入批次
-- ============================================================
CREATE TABLE IF NOT EXISTS demo_import_job (
    job_id           UUID PRIMARY KEY,
    filename         TEXT        NOT NULL DEFAULT '',
    client_ip        TEXT        NOT NULL DEFAULT '',
    template_version TEXT        NOT NULL DEFAULT 'v1',
    total_rows       INTEGER     NOT NULL DEFAULT 0,
    ok_rows          INTEGER     NOT NULL DEFAULT 0,
    warn_rows        INTEGER     NOT NULL DEFAULT 0,
    error_rows       INTEGER     NOT NULL DEFAULT 0,
    -- previewed  → 已解析、未入库（默认）
    -- committed  → 已入库
    -- superseded → 同 IP 后续又提交了同一批，前一批作废（仅标记，不删数据）
    status           TEXT        NOT NULL DEFAULT 'previewed',
    created_at       TIMESTAMPTZ NOT NULL DEFAULT now(),
    committed_at     TIMESTAMPTZ,
    CONSTRAINT demo_import_job_status_chk
        CHECK (status IN ('previewed', 'committed', 'superseded'))
);

COMMENT ON TABLE demo_import_job IS '公开演示导入批次：一次上传一条';

-- 按 IP 数当天次数（限流）与最近批次列表都走这个索引
CREATE INDEX IF NOT EXISTS idx_demo_import_job_ip_time
    ON demo_import_job (client_ip, created_at DESC);


-- ============================================================
-- 行级校验结果
-- ============================================================
CREATE TABLE IF NOT EXISTS demo_import_row (
    id        BIGSERIAL PRIMARY KEY,
    job_id    UUID    NOT NULL REFERENCES demo_import_job(job_id) ON DELETE CASCADE,
    row_no    INTEGER NOT NULL,          -- 表格里的行号（含表头，从 2 开始）
    name      TEXT    NOT NULL DEFAULT '',
    -- ok              正常，可导入
    -- warn            能导入，但有可疑值（如能量偏高、缺来源）
    -- error           不能导入
    -- duplicate_file  文件内重名，只留首行
    -- duplicate_db    演示库里已有同名，导入会覆盖
    verdict   TEXT    NOT NULL DEFAULT 'ok',
    messages  JSONB   NOT NULL DEFAULT '[]'::jsonb,
    payload   JSONB   NOT NULL DEFAULT '{}'::jsonb,
    CONSTRAINT demo_import_row_verdict_chk
        CHECK (verdict IN ('ok', 'warn', 'error', 'duplicate_file', 'duplicate_db'))
);

CREATE INDEX IF NOT EXISTS idx_demo_import_row_job
    ON demo_import_row (job_id, row_no);


-- ============================================================
-- 演示食物库（导入结果，页面上展示的就是这张表）
-- ============================================================
CREATE TABLE IF NOT EXISTS demo_food (
    id               BIGSERIAL PRIMARY KEY,
    name             TEXT NOT NULL,
    -- 归一化名做唯一键：去空格、全角转半角、去括号内容 → 同一食物重复导入走 UPDATE
    name_norm        TEXT NOT NULL UNIQUE,
    aliases          TEXT[] NOT NULL DEFAULT '{}',
    category         TEXT NOT NULL DEFAULT '其他',
    nature           TEXT,
    english_name     TEXT,
    scientific_name  TEXT,
    -- 键是稳定英文名（energy_kcal / calcium_mg / vitamin_c_mg ...），
    -- 值 {"v": 12.3, "nid": "NUT_00006", "unit": "mg"}
    -- 用 JSONB 存全部营养素，不按列摊开：模板以后加一列不用改表结构，
    -- 代价是排序要在应用层做 —— 演示规模（几百行）完全够。
    nutrients        JSONB NOT NULL DEFAULT '{}'::jsonb,
    source           TEXT NOT NULL DEFAULT '',
    note             TEXT NOT NULL DEFAULT '',
    job_id           UUID REFERENCES demo_import_job(job_id) ON DELETE SET NULL,
    created_at       TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at       TIMESTAMPTZ NOT NULL DEFAULT now()
);

COMMENT ON TABLE demo_food IS '公开演示导入的食物。与 kg_food_bundle/主图完全隔离，可整体清空';

CREATE INDEX IF NOT EXISTS idx_demo_food_created ON demo_food (created_at DESC);
CREATE INDEX IF NOT EXISTS idx_demo_food_category ON demo_food (category);

-- 名称模糊搜索：pg_trgm 在 002_business.sql 里已建（若没有则跳过，不阻塞迁移）
DO $$
BEGIN
    CREATE INDEX IF NOT EXISTS idx_demo_food_name_trgm
        ON demo_food USING gin (name gin_trgm_ops);
EXCEPTION WHEN undefined_object THEN
    RAISE NOTICE 'pg_trgm 未安装，跳过 demo_food 名称 GIN 索引';
END $$;

COMMIT;
