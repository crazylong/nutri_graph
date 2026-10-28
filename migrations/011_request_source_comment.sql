-- source 实际取值是 chat | compare | admin。005 里的注释还写着 chat | api。
-- 已应用的迁移不能改文件，这里只更新列注释。

BEGIN;

COMMENT ON COLUMN kg_ingredient_request.source IS 'chat | compare | admin';

COMMIT;
