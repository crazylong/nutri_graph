-- 016_candidate_amount.sql —— 边候选把「数值 + 单位」存下来
--
-- 背景：营养边的数值一直只存在于 claim_text 里（「黑豆每 100g 含蛋白质 36.49 g」），
-- 关系上只有一句自然语言。接口要给前端画表格、要算「哪个食材含铁更高」，
-- 就只能回过头用正则从这句话里抠数字（kg/subgraph.py::parse_amount）。
--
-- 那套正则今天够用，但它把**文本格式当成了接口契约**：换一种说法、加一个括号、
-- 单位写成「占总能量百分比」，就抠不出来或者抠错。这次新增的三种脂肪酸
-- （饱和/单不饱和/多不饱和）单位是 %E，正则里只认得到 %，正好是这个问题的实例。
--
-- 于是把数值和单位提到列上：写库时就有，读的时候不用猜。
-- 保留 claim_text 不变（它是给人看的断言原文，也是现有提示词的输入）。
--
-- 兼容：老边没有 amount，读取端继续走「有 amount 用 amount，没有就回捞文本」，
-- 不需要一次性回填全部历史边。

BEGIN;

ALTER TABLE kg_candidate ADD COLUMN IF NOT EXISTS amount DOUBLE PRECISION;
ALTER TABLE kg_candidate ADD COLUMN IF NOT EXISTS unit   TEXT NOT NULL DEFAULT '';

COMMENT ON COLUMN kg_candidate.amount IS
    'CONTAINS_NUTRIENT 的数值（每 100g）；NULL = 该关系没有数值，不是 0';
COMMENT ON COLUMN kg_candidate.unit IS
    '数值单位：g / mg / μg / kcal / %E（占总能量百分比）';

COMMIT;
