-- 004_align_kg_candidate.sql —— 给老库补齐 kg_candidate 缺失的列
--
-- 背景：kg_candidate 最早由 scripts/init_pg.py 创建，当时没有 dst_name 列；
-- 002_business.sql 里把这一列写进了建表语句，但 CREATE TABLE IF NOT EXISTS
-- 对**已经存在**的表不会新增列 —— 于是老库跑完 002 仍然缺列，
-- 而 kg/review.py 与 api/admin.py 的 SELECT 里都用到了 dst_name，会直接报错。
--
-- 这条就是「为什么要有迁移机制」的标准案例：
--   新库   → 002 建表时已有该列，这里空操作
--   老库   → 这里补上，两条路径最终结构一致
--
-- 以后凡是给已存在的表加列 / 改类型，都必须是新的 ALTER 迁移，
-- 不要回头改已经应用过的 002（会让不同环境的结构产生分叉）。

BEGIN;

ALTER TABLE kg_candidate ADD COLUMN IF NOT EXISTS dst_name TEXT DEFAULT '';

COMMIT;
