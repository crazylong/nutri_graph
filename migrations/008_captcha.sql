-- 008_captcha.sql —— 图形验证码（防刷短信的第二道闸）
-- 每个 challenge 存答案哈希（不存明文），单次消费 + 次数限制。

CREATE TABLE IF NOT EXISTS captcha_challenge (
    id            BIGSERIAL PRIMARY KEY,
    challenge_id  TEXT NOT NULL UNIQUE,          -- 随机 token，前端凭它提交答案
    answer_hash   TEXT NOT NULL,                 -- HMAC(pepper, challenge_id|answer)
    created_at    TIMESTAMPTZ NOT NULL DEFAULT now(),
    expires_at    TIMESTAMPTZ NOT NULL,
    attempts      INT NOT NULL DEFAULT 0,        -- 已尝试次数
    consumed_at   TIMESTAMPTZ,                   -- 答对即消费，不可复用
    ip            TEXT NOT NULL DEFAULT ''       -- 记录来源 IP，便于审计
);

CREATE INDEX IF NOT EXISTS idx_captcha_challenge_ip_created
    ON captcha_challenge (ip, created_at);
