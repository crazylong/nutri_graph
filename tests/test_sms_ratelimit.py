"""频控与锁定"""
from __future__ import annotations

import config
from tests.helpers import login, rand_phone, requires_pg, send_code

pytestmark = requires_pg


def test_same_phone_interval_limited(client):
    phone = rand_phone()
    send_code(client, phone)

    r = client.post("/auth/sms", json={"phone": phone})
    assert r.status_code == 429
    detail = r.json()["detail"]
    assert detail["code"] == "too_frequent"
    assert int(r.headers["retry-after"]) > 0


def test_verify_failures_lock_the_phone(client, monkeypatch):
    monkeypatch.setattr(config, "SMS_MAX_VERIFY_FAILS", 3)
    phone = rand_phone()
    send_code(client, phone)

    statuses = [
        client.post("/auth/token", json={"phone": phone, "code": "000000"}).status_code
        for _ in range(3)
    ]
    assert statuses == [401, 401, 401]

    r = client.post("/auth/token", json={"phone": phone, "code": "000000"})
    assert r.status_code == 429
    assert r.json()["detail"]["code"] == "locked"
    assert int(r.headers["retry-after"]) == config.SMS_LOCK_MINUTES * 60

    # 锁定期间即便验证码正确也进不来
    r = client.post("/auth/token", json={"phone": phone, "code": "123456"})
    assert r.status_code == 429


def test_remaining_attempts_decreases(client, monkeypatch):
    monkeypatch.setattr(config, "SMS_MAX_VERIFY_FAILS", 5)
    phone = rand_phone()
    send_code(client, phone)

    r = client.post("/auth/token", json={"phone": phone, "code": "000000"})
    assert r.json()["detail"]["remaining_attempts"] == config.SMS_MAX_VERIFY_FAILS - 1


def test_phone_daily_quota(client, monkeypatch):
    """同号单日上限：把间隔调成 0，只留日配额生效。"""
    monkeypatch.setattr(config, "SMS_INTERVAL_S", 0)
    monkeypatch.setattr(config, "SMS_MAX_PER_PHONE_HOUR", 1000)
    monkeypatch.setattr(config, "SMS_MAX_PER_PHONE_DAY", 3)

    phone = rand_phone()
    for _ in range(3):
        send_code(client, phone)

    r = client.post("/auth/sms", json={"phone": phone})
    assert r.status_code == 429
    assert r.json()["detail"]["code"] == "quota_exceeded"


def test_global_circuit_breaker(client, monkeypatch):
    monkeypatch.setattr(config, "SMS_INTERVAL_S", 0)
    monkeypatch.setattr(config, "SMS_MAX_PER_PHONE_HOUR", 1000)
    monkeypatch.setattr(config, "SMS_MAX_PER_IP_HOUR", 1000)

    import psycopg

    with psycopg.connect(config.PG_DSN) as conn:
        used = conn.execute(
            "SELECT count(*) FROM sms_send_log WHERE ok "
            "AND created_at > now() - interval '1 day'"
        ).fetchone()[0]

    monkeypatch.setattr(config, "SMS_MAX_GLOBAL_DAY", used + 1)

    send_code(client, rand_phone())          # 用掉最后一个名额
    r = client.post("/auth/sms", json={"phone": rand_phone()})
    assert r.status_code == 429
    assert r.json()["detail"]["code"] == "global_limit"


def test_successful_login_is_not_blocked_by_prior_failures(client):
    """换个手机号不该被上一个号的失败次数牵连。"""
    bad = rand_phone()
    send_code(client, bad)
    for _ in range(2):
        client.post("/auth/token", json={"phone": bad, "code": "000000"})

    tokens = login(client)
    assert tokens["access_token"]
