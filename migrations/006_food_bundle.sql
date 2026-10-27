-- 006_food_bundle.sql —— 抓取任务与审核单元（Phase 9.2）
--
-- 三张表各管一段，不要混：
--   kg_fetch_task       一个食材的**抓取任务**（幂等、重试、预算、可恢复）
--   kg_food_bundle      一次抓取的**产物快照**（整体评分 / 硬否决 / 审核状态）
--   kg_food_bundle_item 产物里的**逐项**（字段与营养素），审核可以逐条通过/驳回
--
-- 为什么 bundle 不直接写主图：
--   审核通过后由 9.3 转成 kg_candidate，再走 kg.merge。
--   主图只能有一个写入口，否则「这条边是谁加的」以后查不清。
--
-- 单源现实：当前只接 USDA FDC，source_count 恒为 1，
-- 评分会因此封顶在 0.88 < FOOD_AUTO_APPROVE_MIN_SCORE(0.9)，
-- 所以**所有 bundle 都会进 pending_review** —— 这是刻意设计的保守行为，
-- 等接入第二个来源（中国食物成分表离线导入）才会真正有自动通过的通道。

BEGIN;

CREATE TABLE IF NOT EXISTS kg_fetch_task (
    id           BIGSERIAL PRIMARY KEY,
    request_id   BIGINT NOT NULL REFERENCES kg_ingredient_request (id) ON DELETE CASCADE,
    food_key     TEXT        NOT NULL UNIQUE,   -- 一个食材只允许一个任务
    status       TEXT        NOT NULL DEFAULT 'queued',
                                                -- queued | running | done | failed | rejected
    attempts     INT         NOT NULL DEFAULT 0,
    last_error   TEXT        NOT NULL DEFAULT '',
    budget_day   DATE        NOT NULL DEFAULT CURRENT_DATE,
    started_at   TIMESTAMPTZ,
    finished_at  TIMESTAMPTZ,
    next_retry_at TIMESTAMPTZ,
    created_at   TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS idx_fetch_task_status ON kg_fetch_task (status);
CREATE INDEX IF NOT EXISTS idx_fetch_task_day    ON kg_fetch_task (budget_day, status);

CREATE TABLE IF NOT EXISTS kg_food_bundle (
    id              BIGSERIAL PRIMARY KEY,
    request_id      BIGINT REFERENCES kg_ingredient_request (id) ON DELETE SET NULL,
    food_key        TEXT        NOT NULL,
    canonical_name  TEXT        NOT NULL DEFAULT '',
    english_name    TEXT        NOT NULL DEFAULT '',
    scientific_name TEXT        NOT NULL DEFAULT '',
    category        TEXT        NOT NULL DEFAULT '',
    nature          TEXT        NOT NULL DEFAULT '',
    source_key      TEXT        NOT NULL DEFAULT '',   -- usda_fdc | china_fcd | ...
    external_id     TEXT        NOT NULL DEFAULT '',   -- fdcId 等外部主键，便于回溯
    source_desc     TEXT        NOT NULL DEFAULT '',   -- 原始 description，人审时看这个
    score_total     FLOAT       NOT NULL DEFAULT 0,
    score_detail    JSONB       NOT NULL DEFAULT '{}',
    source_count    INT         NOT NULL DEFAULT 0,
    sources         JSONB       NOT NULL DEFAULT '[]',
    hard_veto       TEXT        NOT NULL DEFAULT '',
    status          TEXT        NOT NULL DEFAULT 'draft',
                                                    -- draft|verified|pending_review|approved|rejected
    reviewer        TEXT        NOT NULL DEFAULT '',
    reviewed_at     TIMESTAMPTZ,
    reject_reason   TEXT        NOT NULL DEFAULT '',
    created_at      TIMESTAMPTZ NOT NULL DEFAULT now(),
    UNIQUE (request_id)          -- 一个请求一份产物，重跑覆盖而不是堆叠
);
CREATE INDEX IF NOT EXISTS idx_bundle_status ON kg_food_bundle (status);

CREATE TABLE IF NOT EXISTS kg_food_bundle_item (
    id             BIGSERIAL PRIMARY KEY,
    bundle_id      BIGINT NOT NULL REFERENCES kg_food_bundle (id) ON DELETE CASCADE,
    item_type      TEXT   NOT NULL DEFAULT 'field',  -- field | nutrient
    field_name     TEXT   NOT NULL,
    field_value    TEXT   NOT NULL DEFAULT '',
    unit           TEXT   NOT NULL DEFAULT '',
    source_locator TEXT   NOT NULL DEFAULT '',       -- 如 fdc:174270#1003，可回溯到原始响应
    grounded       BOOLEAN NOT NULL DEFAULT true,    -- 该项能否追溯到来源
    item_status    TEXT   NOT NULL DEFAULT 'pending',-- pending | passed | rejected
    created_at     TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS idx_bundle_item_bundle ON kg_food_bundle_item (bundle_id);

COMMIT;
