-- 019_sensitive_review.sql —— bundle 上的敏感词审计字段
--
-- 三个批准口（approve_bundle / CLI / _set_status）共用这套字段。
-- 记下来的目的只有一个：事后能回答"这条数据当时为什么被放过"。
--
-- sensitive_word_version 记的是**整表**版本（sensitive_word_version.version），
-- 不是 sensitive_word.version —— 后者是单行计数，只服务于单行回滚。

ALTER TABLE kg_food_bundle ADD COLUMN IF NOT EXISTS sensitive_hits JSONB DEFAULT '[]';
ALTER TABLE kg_food_bundle ADD COLUMN IF NOT EXISTS sensitive_reviewed_by TEXT DEFAULT '';
ALTER TABLE kg_food_bundle ADD COLUMN IF NOT EXISTS sensitive_reviewed_at TIMESTAMPTZ;
ALTER TABLE kg_food_bundle ADD COLUMN IF NOT EXISTS sensitive_word_version INT DEFAULT 0;
