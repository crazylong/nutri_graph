-- 005_food_request.sql —— 未收录食材的登记表（Phase 9.1）
--
-- 设计要点：
--   * food_key 是**归一后的中心词**（黑豆粥 → 黑豆），不是用户原话 ——
--     否则「黑豆」「黑豆粥」「凉拌黑豆」会变成三条，把抓取预算三倍消耗掉
--   * 一张表同时承担「幂等去重」和「需求热度」：hit_count 就是有多少人问过
--   * 抓取任务（kg_fetch_task）与产物（kg_food_bundle）在 9.2 单独建，
--     这里只负责「登记」，不产生任何抓取副作用
--
-- 状态机：
--   queued → fetching → fetched → pending_review → approved | rejected
--   9.1 只会写入 queued；后续阶段推进状态

BEGIN;

CREATE TABLE IF NOT EXISTS kg_ingredient_request (
    id                BIGSERIAL PRIMARY KEY,
    food_key          TEXT        NOT NULL UNIQUE,   -- 归一后的中心词
    raw_mention       TEXT        NOT NULL DEFAULT '',  -- 用户原话，便于回溯
    normalized        TEXT        NOT NULL DEFAULT '',
    requested_by      TEXT        NOT NULL DEFAULT '',  -- user_id 或 anon:xxx
    source            TEXT        NOT NULL DEFAULT 'chat',  -- chat | api
    hit_count         INT         NOT NULL DEFAULT 1,
    status            TEXT        NOT NULL DEFAULT 'queued',
    reject_reason     TEXT        NOT NULL DEFAULT '',
    last_requested_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    created_at        TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS idx_food_request_status ON kg_ingredient_request (status);
CREATE INDEX IF NOT EXISTS idx_food_request_user
    ON kg_ingredient_request (requested_by, created_at DESC);
CREATE INDEX IF NOT EXISTS idx_food_request_time
    ON kg_ingredient_request (created_at DESC);

COMMIT;
