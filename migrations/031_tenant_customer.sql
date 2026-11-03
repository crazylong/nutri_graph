-- 031_tenant_customer.sql —— 员工租户、企业客户，并清掉存量食疗前台账号
--
-- 只跑一次。role=user 的 app_user 视为食疗前台账号，连同名下食疗会话删除。
-- role=admin 保留，并挂到 default_renter。

BEGIN;

ALTER TABLE app_user ADD COLUMN IF NOT EXISTS username TEXT NOT NULL DEFAULT '';

CREATE TABLE IF NOT EXISTS tenant (
    name       TEXT PRIMARY KEY,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    CONSTRAINT tenant_name_re CHECK (name ~ '^[A-Za-z0-9_]{1,32}$')
);

CREATE TABLE IF NOT EXISTS tenant_user (
    tenant_name TEXT NOT NULL REFERENCES tenant (name) ON DELETE CASCADE,
    user_id     UUID NOT NULL REFERENCES app_user (user_id) ON DELETE CASCADE,
    PRIMARY KEY (tenant_name, user_id)
);

CREATE TABLE IF NOT EXISTS customer (
    user_id       UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    phone_enc     TEXT        NOT NULL,
    phone_hash    TEXT        NOT NULL UNIQUE,
    phone_mask    TEXT        NOT NULL DEFAULT '',
    status        TEXT        NOT NULL DEFAULT 'active',
    created_at    TIMESTAMPTZ NOT NULL DEFAULT now(),
    last_login_at TIMESTAMPTZ
);

CREATE TABLE IF NOT EXISTS customer_refresh (
    jti         UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    user_id     UUID        NOT NULL REFERENCES customer (user_id) ON DELETE CASCADE,
    token_hash  TEXT        NOT NULL UNIQUE,
    family_id   UUID        NOT NULL,
    expires_at  TIMESTAMPTZ NOT NULL,
    revoked_at  TIMESTAMPTZ,
    replaced_by UUID,
    created_at  TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS idx_customer_refresh_user ON customer_refresh (user_id);
CREATE INDEX IF NOT EXISTS idx_customer_refresh_family ON customer_refresh (family_id);

CREATE TABLE IF NOT EXISTS customer_consent (
    id             BIGSERIAL PRIMARY KEY,
    user_id        UUID        NOT NULL REFERENCES customer (user_id) ON DELETE CASCADE,
    policy_version TEXT        NOT NULL,
    agreed_at      TIMESTAMPTZ NOT NULL DEFAULT now(),
    ip             TEXT        NOT NULL DEFAULT '',
    UNIQUE (user_id, policy_version)
);

INSERT INTO tenant (name) VALUES ('default_renter') ON CONFLICT DO NOTHING;

INSERT INTO tenant (name)
SELECT DISTINCT tenant_id
FROM kb_document
WHERE tenant_id ~ '^[A-Za-z0-9_]{1,32}$'
ON CONFLICT DO NOTHING;

DELETE FROM message
WHERE session_id IN (
    SELECT id FROM session
    WHERE user_id IN (SELECT user_id::text FROM app_user WHERE role = 'user')
);

DELETE FROM session
WHERE user_id IN (SELECT user_id::text FROM app_user WHERE role = 'user');

DELETE FROM user_profile
WHERE user_id IN (SELECT user_id::text FROM app_user WHERE role = 'user');

DELETE FROM memory_fact
WHERE user_id IN (SELECT user_id::text FROM app_user WHERE role = 'user');

DELETE FROM app_user WHERE role = 'user';

INSERT INTO tenant_user (tenant_name, user_id)
SELECT 'default_renter', user_id FROM app_user
ON CONFLICT DO NOTHING;

COMMIT;
