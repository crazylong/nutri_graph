-- 030_sms_verify_code.sql —— 管理端短信记录要能看到当次验证码
--
-- 校验仍走哈希或阿里云托管，这列只给后台查看。历史行没有明文，保持空串。

BEGIN;

ALTER TABLE sms_send_log
    ADD COLUMN IF NOT EXISTS verify_code TEXT NOT NULL DEFAULT '';

COMMIT;
