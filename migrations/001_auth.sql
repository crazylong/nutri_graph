-- 001_auth.sql —— 身份与访问控制（needs 13）
--
-- 设计要点：
--   * 手机号绝不做主键：phone_hash 负责唯一索引与查询，phone_enc 负责还原，phone_mask 负责展示
--   * 验证码：走短信认证（PNVS）时由平台托管，sms_code 只服务本地 console provider
--   * 频控与锁定：平台不管「短信轰炸」，这两张日志表是唯一的防线，也是审计依据
--   * refresh_token 存哈希 + family_id：支持旋转与「重放即整族吊销」

BEGIN;

-- ---------- 账号主表 ----------
CREATE TABLE IF NOT EXISTS app_user (
    user_id       UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    phone_enc     TEXT        NOT NULL,              -- Fernet 密文
    phone_hash    TEXT        NOT NULL UNIQUE,       -- HMAC-SHA256(pepper, phone)
    phone_mask    TEXT        NOT NULL DEFAULT '',   -- 138****8888
    status        TEXT        NOT NULL DEFAULT 'active',  -- active | locked | deleted
    role          TEXT        NOT NULL DEFAULT 'user',    -- user | admin
    created_at    TIMESTAMPTZ NOT NULL DEFAULT now(),
    last_login_at TIMESTAMPTZ,
    deleted_at    TIMESTAMPTZ
);
CREATE INDEX IF NOT EXISTS idx_app_user_status ON app_user (status);

-- ---------- 验证码（仅 console provider 使用）----------
CREATE TABLE IF NOT EXISTS sms_code (
    id          BIGSERIAL PRIMARY KEY,
    phone_hash  TEXT        NOT NULL,
    code_hash   TEXT        NOT NULL,                -- HMAC，不存明文
    purpose     TEXT        NOT NULL DEFAULT 'login',
    expires_at  TIMESTAMPTZ NOT NULL,
    consumed_at TIMESTAMPTZ,
    created_at  TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS idx_sms_code_lookup ON sms_code (phone_hash, purpose, created_at DESC);

-- ---------- 发送流水（频控依据 + 成本审计）----------
CREATE TABLE IF NOT EXISTS sms_send_log (
    id              BIGSERIAL PRIMARY KEY,
    phone_hash      TEXT        NOT NULL,
    ip              TEXT        NOT NULL DEFAULT '',
    device_fp       TEXT        NOT NULL DEFAULT '',
    purpose         TEXT        NOT NULL DEFAULT 'login',
    provider        TEXT        NOT NULL DEFAULT '',
    provider_msg_id TEXT        NOT NULL DEFAULT '',
    ok              BOOLEAN     NOT NULL DEFAULT true,
    error           TEXT        NOT NULL DEFAULT '',
    created_at      TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS idx_sms_send_phone  ON sms_send_log (phone_hash, created_at DESC);
CREATE INDEX IF NOT EXISTS idx_sms_send_ip     ON sms_send_log (ip, created_at DESC);
CREATE INDEX IF NOT EXISTS idx_sms_send_device ON sms_send_log (device_fp, created_at DESC);
CREATE INDEX IF NOT EXISTS idx_sms_send_time   ON sms_send_log (created_at DESC);

-- ---------- 校验流水（失败锁定依据）----------
CREATE TABLE IF NOT EXISTS sms_verify_attempt (
    id         BIGSERIAL PRIMARY KEY,
    phone_hash TEXT        NOT NULL,
    ip         TEXT        NOT NULL DEFAULT '',
    purpose    TEXT        NOT NULL DEFAULT 'login',
    ok         BOOLEAN     NOT NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS idx_sms_verify_phone ON sms_verify_attempt (phone_hash, created_at DESC);

-- ---------- 刷新令牌 ----------
CREATE TABLE IF NOT EXISTS refresh_token (
    jti         UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    user_id     UUID        NOT NULL REFERENCES app_user (user_id) ON DELETE CASCADE,
    token_hash  TEXT        NOT NULL UNIQUE,         -- sha256(raw)
    family_id   UUID        NOT NULL,                -- 同一条登录链共享
    expires_at  TIMESTAMPTZ NOT NULL,
    revoked_at  TIMESTAMPTZ,
    replaced_by UUID,
    created_at  TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS idx_refresh_user   ON refresh_token (user_id);
CREATE INDEX IF NOT EXISTS idx_refresh_family ON refresh_token (family_id);

-- ---------- 隐私政策同意（合规：单独同意 + 版本）----------
CREATE TABLE IF NOT EXISTS user_consent (
    id             BIGSERIAL PRIMARY KEY,
    user_id        UUID        NOT NULL REFERENCES app_user (user_id) ON DELETE CASCADE,
    policy_version TEXT        NOT NULL,
    agreed_at      TIMESTAMPTZ NOT NULL DEFAULT now(),
    ip             TEXT        NOT NULL DEFAULT '',
    UNIQUE (user_id, policy_version)
);

COMMIT;
