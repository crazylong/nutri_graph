-- 017_admin_import.sql —— 管理员导入通道
--
-- 管理员在后台 /console「批量导入」走的接口（api/admin_import.py）需要按
-- 管理员身份计额，而不是按 IP。公开导入（/kg/import）沿用 client_ip，
-- 管理员导入把身份写进 reviewer 列，两者互不干扰。

BEGIN;

ALTER TABLE kg_import_job
    ADD COLUMN IF NOT EXISTS reviewer TEXT NOT NULL DEFAULT '';

COMMENT ON COLUMN kg_import_job.reviewer IS
    '导入发起人：管理员导入写 user_id，公开导入留空（走 client_ip 计额）';

CREATE INDEX IF NOT EXISTS idx_kg_import_job_reviewer
    ON kg_import_job (reviewer, created_at DESC);

COMMIT;
