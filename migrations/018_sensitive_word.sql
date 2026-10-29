-- 018_sensitive_word.sql —— 敏感词表、词表版本、命中审计
--
-- 为什么词表放库里而不是写死在 .py：
--   合规口径会变，写死意味着改一个词要发版。且 action（block / review）需要
--   按字段分级 —— 同一个词在"名称"里要拦、在"描述"里只提示，这只有数据能表达。
--   代码里保留一份内置兜底（kg/sensitive.py 的 _BUILTIN），库不可用时防护不失效。
--
-- 三张表的分工：
--   sensitive_word         词本身（action 是**默认**动作，按字段折算后可能变严：见代码）
--   sensitive_word_version 整表版本号。审计里记的是它，用来回溯"当时用了哪套词表"
--   sensitive_hit_log      命中审计。**不存原词**，只存 hash —— 日志会流转、会被备份

CREATE TABLE IF NOT EXISTS sensitive_word (
    id          BIGSERIAL PRIMARY KEY,
    word        TEXT NOT NULL,
    category    TEXT NOT NULL,          -- political | medical_claim | marketing | superlative
    action      TEXT NOT NULL DEFAULT 'review',  -- block | review（三档里的 flag 没有行为，不建）
    severity    INT  NOT NULL DEFAULT 2,         -- 1 低 2 中 3 高
    enabled     BOOLEAN NOT NULL DEFAULT true,
    version     INT  NOT NULL DEFAULT 1,         -- 单行回滚用，不是整表版本
    promoted_by TEXT NOT NULL DEFAULT '',        -- 谁把它升成 block 的
    promoted_at TIMESTAMPTZ,
    note        TEXT NOT NULL DEFAULT '',        -- 升级依据（当时的 hits/rejected/passed）
    created_at  TIMESTAMPTZ NOT NULL DEFAULT now(),
    UNIQUE (word, category)
);

CREATE TABLE IF NOT EXISTS sensitive_word_version (
    id          BIGSERIAL PRIMARY KEY,
    version     INT NOT NULL,
    updated_by  TEXT NOT NULL DEFAULT '',
    updated_at  TIMESTAMPTZ NOT NULL DEFAULT now(),
    note        TEXT NOT NULL DEFAULT ''
);

-- bundle_id 不能省：收紧报表要按词统计「被驳回 / 被批准」的比例，
-- 没有它 join 不到审核结论，那两列算不出来（收紧就只能靠感觉）。
CREATE TABLE IF NOT EXISTS sensitive_hit_log (
    id           BIGSERIAL PRIMARY KEY,
    scene        TEXT NOT NULL,         -- input | output | import | review
    category     TEXT NOT NULL,
    action       TEXT NOT NULL,         -- 当时实际采取的动作：block | review
    field        TEXT NOT NULL DEFAULT '',
    term_hash    TEXT NOT NULL,         -- sha256(word)[:16]
    source       TEXT NOT NULL DEFAULT '',
    bundle_id    BIGINT,                -- review 场景必填
    word_version INT NOT NULL DEFAULT 1,
    created_at   TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS idx_shl_word ON sensitive_hit_log (term_hash, created_at);
CREATE INDEX IF NOT EXISTS idx_shl_bundle ON sensitive_hit_log (bundle_id);

INSERT INTO sensitive_word_version (version, updated_by, note)
SELECT 1, 'migration-018', '词表首版'
WHERE NOT EXISTS (SELECT 1 FROM sensitive_word_version);

-- ------------------------------------------------------------
-- 种子数据
--
-- 来源：合规手册〇节的红线（治疗 / 治愈 / 根治 / 处方 / 诊断 / 疗效）。
-- political 一类由运营维护，这里只放最小可用集合 —— 自建词表追不上监管口径，
-- 真要兜底得接第三方（计划 §1.8，v3 不接）。
--
-- action 的含义是**描述类字段**上的默认动作；名称类字段一律按 block 处理，
-- 政治类在任何字段都是 block（见 kg/sensitive.py 的 _effective_action）。
-- ------------------------------------------------------------
INSERT INTO sensitive_word (word, category, action, severity, note) VALUES
    -- 政治类：任何字段命中即阻断，且不进审核队列（不进库就不留传播面）
    ('台独',   'political', 'block', 3, '合规红线'),
    ('法轮功', 'political', 'block', 3, '合规红线'),
    ('达赖',   'political', 'block', 3, '合规红线'),
    -- 疗效断言：描述类先 review（中医文献里"辅助治疗"这类说法确实存在），
    -- 观察一周再按 §3.6 的四步收紧
    ('治疗',   'medical_claim', 'review', 3, '合规红线'),
    ('治愈',   'medical_claim', 'review', 3, '合规红线'),
    ('根治',   'medical_claim', 'review', 3, '合规红线'),
    ('处方',   'medical_claim', 'review', 3, '合规红线'),
    ('诊断',   'medical_claim', 'review', 3, '合规红线'),
    ('疗效',   'medical_claim', 'review', 3, '合规红线'),
    ('药到病除', 'medical_claim', 'review', 3, ''),
    ('降三高', 'medical_claim', 'review', 3, ''),
    ('抗癌',   'medical_claim', 'review', 3, ''),
    ('包治',   'medical_claim', 'review', 3, ''),
    -- 营销导流
    ('加微信', 'marketing', 'review', 2, ''),
    ('微商',   'marketing', 'review', 2, ''),
    ('代理',   'marketing', 'review', 2, ''),
    ('包邮',   'marketing', 'review', 2, ''),
    ('私聊',   'marketing', 'review', 2, ''),
    -- 绝对化用语
    ('最佳',   'superlative', 'review', 1, ''),
    ('最好',   'superlative', 'review', 1, ''),
    ('国家级', 'superlative', 'review', 1, ''),
    ('最有效', 'superlative', 'review', 1, '')
ON CONFLICT (word, category) DO NOTHING;
