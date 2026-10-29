-- 022_sensitive_word_seed2.sql —— 补齐完成条件点名、但 018 没种上的词
--
-- 不改 018：scripts/migrate.py 对已执行文件做校验，改了会报警。
-- 不用 021：plan_v4 已把 021_web_answer.sql 占住。
--
-- 「功效」是 name_only：只在名称里拦。描述和回答里不产生命中，
-- 否则输出扫描会按类别把「有健脾功效」整句删掉。
-- 不收「电话」：会命中「联系电话」。

INSERT INTO sensitive_word (word, category, action, severity, note) VALUES
    ('毛爷爷', 'political',  'block',     3, '合规红线（完成条件点名的样例）'),
    ('销售',   'marketing',  'review',    2, ''),
    ('下单',   'marketing',  'review',    2, ''),
    ('功效',   'name_guard', 'name_only', 2, '只在名称拦截，描述与回答放行')
ON CONFLICT (word, category) DO NOTHING;

INSERT INTO sensitive_word_version (version, updated_by, note)
SELECT 2, 'migration-022', '补齐：毛爷爷 / 销售 / 下单 / 功效'
WHERE NOT EXISTS (SELECT 1 FROM sensitive_word_version WHERE version = 2);
