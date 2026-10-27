-- 007_promote.sql —— 审核闭环（Phase 9.3）
--
-- 这一步要解决的核心问题：**bundle 里是一个整块的食材画像，
-- 但主图里是「节点 + 边」两类东西**，原来只有 kg_candidate（边）一张候选表，
-- 新食材根本没有地方表达「要新建一个 Ingredient 节点」。
--
-- 于是主图出现两个写入口：kg.merge 写边、某个脚本偷偷写节点。
-- 一旦有两个入口，「这条边是谁加的、为什么加」以后永远查不清。
--
-- 所以这里补齐「节点候选」，让 kg.merge 成为唯一写入口：
--   kg_node_candidate  —— 待新建的节点（Ingredient / Nutrient）
--   kg_candidate       —— 待新建/更新的边（已有表，这里补两个字段）
--   两者都只由 kg.merge 消费，审核阶段只负责「把 bundle 翻译成候选」。

BEGIN;

-- ---------- bundle 记住自己最终落到哪个节点 ----------
-- 审核时可能把「黑豆」挂到既有节点（如已存在的黑大豆）而不是新建，
-- 这个决议必须落库，否则重跑 merge 就不知道往哪挂了。
ALTER TABLE kg_food_bundle ADD COLUMN IF NOT EXISTS canonical_id TEXT NOT NULL DEFAULT '';

-- ---------- 边候选补「可信度分层」所需字段 ----------
-- provisional = 「过了人工审核，但只有单一来源、未交叉验证」
-- 注意它不等于「未审核」：未审核的数据根本不会进候选表。
-- 检索默认仍然返回它（否则刚补录完就查不到，闭环等于断的），
-- 但会降级引用：证据等级封顶、上下文标注、回答加提示。
-- INCLUDE_PROVISIONAL=false 时查询直接排除，用于「只答硬证据」的严格模式。
ALTER TABLE kg_candidate ADD COLUMN IF NOT EXISTS provisional  BOOLEAN NOT NULL DEFAULT false;
ALTER TABLE kg_candidate ADD COLUMN IF NOT EXISTS source_count INT     NOT NULL DEFAULT 0;

-- ---------- 节点候选 ----------
CREATE TABLE IF NOT EXISTS kg_node_candidate (
    id           BIGSERIAL PRIMARY KEY,
    batch_id     TEXT NOT NULL,
    node_type    TEXT NOT NULL,                    -- Ingredient | Nutrient
    canonical_id TEXT NOT NULL,
    name         TEXT NOT NULL DEFAULT '',
    props        JSONB NOT NULL DEFAULT '{}',      -- 只允许白名单内的属性
    aliases      TEXT[] NOT NULL DEFAULT '{}',     -- 审核通过后写入 dict_alias
    bundle_id    BIGINT REFERENCES kg_food_bundle (id) ON DELETE SET NULL,
    status       TEXT NOT NULL DEFAULT 'pending',  -- pending | approved | rejected | merged
    reviewer     TEXT NOT NULL DEFAULT '',
    reviewed_at  TIMESTAMPTZ,
    created_at   TIMESTAMPTZ NOT NULL DEFAULT now(),
    UNIQUE (canonical_id)
);
CREATE INDEX IF NOT EXISTS idx_node_candidate_status ON kg_node_candidate (status);

-- ---------- 新食材的 canonical_id 分配 ----------
-- 为什么从 1000 起：crosswalk 里人工标注的种子是 ING_00001..ING_00125，
-- 跳到 1000 一眼能看出「这是补录进来的」，也避免和将来扩充种子时撞号。
CREATE SEQUENCE IF NOT EXISTS ingredient_cid_seq START WITH 1000;

COMMIT;
