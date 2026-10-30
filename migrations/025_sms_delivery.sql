-- 025_sms_delivery.sql —— 短信送达回执
--
-- 不新建表。频控和后台列表看同一张 sms_send_log。
-- ok 仍是「接口是否受理」，送达看 delivery_status。

BEGIN;

ALTER TABLE sms_send_log
    ADD COLUMN IF NOT EXISTS phone_mask TEXT NOT NULL DEFAULT '',
    ADD COLUMN IF NOT EXISTS biz_id TEXT NOT NULL DEFAULT '',
    ADD COLUMN IF NOT EXISTS delivery_status TEXT NOT NULL DEFAULT 'none',
    ADD COLUMN IF NOT EXISTS err_code TEXT NOT NULL DEFAULT '',
    ADD COLUMN IF NOT EXISTS err_msg TEXT NOT NULL DEFAULT '',
    ADD COLUMN IF NOT EXISTS sms_size INT,
    ADD COLUMN IF NOT EXISTS report_at TIMESTAMPTZ;

CREATE INDEX IF NOT EXISTS idx_sms_send_biz
    ON sms_send_log (biz_id)
    WHERE biz_id <> '';

CREATE INDEX IF NOT EXISTS idx_sms_send_msgid
    ON sms_send_log (provider_msg_id)
    WHERE provider_msg_id <> '';

-- 注销用户的 phone_mask 已是空串，回填之后仍是空，这不是失败。
UPDATE sms_send_log AS s
SET phone_mask = u.phone_mask
FROM app_user AS u
WHERE s.phone_hash = u.phone_hash
  AND s.phone_mask = '';

COMMIT;
