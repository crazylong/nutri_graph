-- 010_profile_attrs.sql —— 画像补人口学与症状
--
-- conditions 继续只放慢性病。失眠、生理期、怀孕进 symptoms，不并进 conditions。
-- 标量列可空，旧行不用回填。

BEGIN;

ALTER TABLE user_profile ADD COLUMN IF NOT EXISTS age INT;
ALTER TABLE user_profile ADD COLUMN IF NOT EXISTS gender TEXT;
ALTER TABLE user_profile ADD COLUMN IF NOT EXISTS height_cm DOUBLE PRECISION;
ALTER TABLE user_profile ADD COLUMN IF NOT EXISTS weight_kg DOUBLE PRECISION;
ALTER TABLE user_profile ADD COLUMN IF NOT EXISTS hobbies TEXT[] DEFAULT '{}';
ALTER TABLE user_profile ADD COLUMN IF NOT EXISTS symptoms TEXT[] DEFAULT '{}';

COMMIT;
