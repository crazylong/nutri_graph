"""短信回执与后台列表。"""
from __future__ import annotations

import logging
from contextlib import contextmanager

import psycopg
import pytest

import config
from security.crypto import phone_hash, phone_mask
from security.ratelimit import record_send
from security.sms import SendResult
from tests.helpers import bearer, login, rand_phone, requires_pg, send_code

pytestmark = requires_pg


def _row(msg_id: str):
    with psycopg.connect(config.PG_DSN) as conn:
        return conn.execute(
            "SELECT ok, delivery_status, biz_id, phone_mask, sms_size, report_at, provider "
            "FROM sms_send_log WHERE provider_msg_id = %s ORDER BY id DESC LIMIT 1",
            (msg_id,),
        ).fetchone()


def test_console_send_is_logged(client):
    phone = rand_phone()
    send_code(client, phone)
    with psycopg.connect(config.PG_DSN) as conn:
        row = conn.execute(
            "SELECT provider, ok, delivery_status, phone_mask FROM sms_send_log "
            "WHERE phone_hash = %s ORDER BY id DESC LIMIT 1",
            (phone_hash(phone),),
        ).fetchone()
    assert row[0] == "console"
    assert row[1] is True
    assert row[2] == "none"
    assert row[3] == phone_mask(phone)


def test_dypns_pending_and_failed(client, monkeypatch):
    phone = rand_phone()

    class Provider:
        name = "dypns"

        def __init__(self, ok):
            self._ok = ok

        def send(self, phone, purpose="login"):
            return SendResult(
                ok=self._ok,
                provider="dypns",
                msg_id="login-" + ("a" if self._ok else "b") * 12,
                biz_id="BIZ1" if self._ok else "",
                error="" if self._ok else "no",
            )

    monkeypatch.setattr("api.auth.get_provider", lambda: Provider(True))
    assert client.post("/auth/sms", json={"phone": phone}).status_code == 200
    row = _row("login-" + "a" * 12)
    assert row[0] is True and row[1] == "pending" and row[2] == "BIZ1"
    assert row[3] == phone_mask(phone)

    monkeypatch.setattr("api.auth.get_provider", lambda: Provider(False))
    monkeypatch.setattr(config, "SMS_INTERVAL_S", 0)
    failed = client.post("/auth/sms", json={"phone": phone})
    assert failed.status_code == 502
    row = _row("login-" + "b" * 12)
    assert row[0] is False and row[1] == "none"


def test_rate_limit_does_not_insert(client):
    phone = rand_phone()
    send_code(client, phone)
    with psycopg.connect(config.PG_DSN) as conn:
        before = conn.execute(
            "SELECT count(*) FROM sms_send_log WHERE phone_hash = %s",
            (phone_hash(phone),),
        ).fetchone()[0]
    assert client.post("/auth/sms", json={"phone": phone}).status_code == 429
    with psycopg.connect(config.PG_DSN) as conn:
        after = conn.execute(
            "SELECT count(*) FROM sms_send_log WHERE phone_hash = %s",
            (phone_hash(phone),),
        ).fetchone()[0]
    assert after == before


def _report(client, token, body):
    return client.post(f"/auth/sms/report?token={token}", json=body)


def test_report_token_and_matching(client, monkeypatch, caplog):
    # 回执编号是固定的，测试库不会在每轮开始清空，先删掉上一轮留下的行。
    with psycopg.connect(config.PG_DSN) as conn:
        conn.execute(
            "DELETE FROM sms_send_log WHERE provider_msg_id IN (%s, %s) OR biz_id IN ('BIZC', 'BIZONLY')",
            ("login-" + "c" * 12, "login-" + "d" * 12),
        )
    monkeypatch.setattr(config, "SMS_REPORT_TOKEN", "")
    with caplog.at_level(logging.WARNING):
        denied = _report(client, "", [{"out_id": "x"}])
    assert denied.status_code == 503
    assert "token" not in denied.text or "回执未配置" in denied.text

    monkeypatch.setattr(config, "SMS_REPORT_TOKEN", "sekret-value")
    caplog.clear()
    with caplog.at_level(logging.WARNING):
        wrong = _report(client, "nope", [{"out_id": "x"}])
    assert wrong.status_code == 404
    assert "接收成功" not in wrong.text
    assert "sekret-value" not in " ".join(rec.message for rec in caplog.records)

    msg_id = "login-" + "c" * 12
    record_send("h1", "1.1.1.1", "", "login", "dypns", msg_id, True, "", "138****0000", "BIZC", "pending")
    ok = _report(client, "sekret-value", [{
        "out_id": msg_id,
        "biz_id": "BIZC",
        "success": True,
        "err_code": "DELIVERED",
        "err_msg": "x" * 300,
        "sms_size": "1",
        "report_time": "2017-01-01 00:00:00",
    }])
    assert ok.status_code == 200
    assert ok.json() == {"code": 0, "msg": "接收成功"}
    row = _row(msg_id)
    assert row[1] == "delivered"
    assert row[4] == 1
    assert row[5].astimezone(__import__("zoneinfo").ZoneInfo("Asia/Shanghai")).strftime("%Y-%m-%d %H:%M:%S") == "2017-01-01 00:00:00"
    with psycopg.connect(config.PG_DSN) as conn:
        err = conn.execute(
            "SELECT err_msg FROM sms_send_log WHERE provider_msg_id = %s",
            (msg_id,),
        ).fetchone()[0]
    assert len(err) == 200

    again = _report(client, "sekret-value", [{
        "out_id": msg_id, "success": False, "sms_size": "1", "report_time": "2017-01-01 00:00:00",
    }])
    assert again.status_code == 200
    with psycopg.connect(config.PG_DSN) as conn:
        count = conn.execute(
            "SELECT count(*) FROM sms_send_log WHERE provider_msg_id = %s",
            (msg_id,),
        ).fetchone()[0]
    assert count == 1

    record_send("h2", "", "", "login", "dypns", "", True, "", "", "BIZONLY", "pending")
    via_biz = _report(client, "sekret-value", [{
        "out_id": "login-" + "d" * 12,
        "biz_id": "BIZONLY",
        "success": True,
        "sms_size": "",
    }])
    assert via_biz.status_code == 200
    with psycopg.connect(config.PG_DSN) as conn:
        size = conn.execute(
            "SELECT sms_size, delivery_status FROM sms_send_log WHERE biz_id = %s",
            ("BIZONLY",),
        ).fetchone()
    assert size[0] is None and size[1] == "delivered"

    missing = _report(client, "sekret-value", [{
        "out_id": "login-" + "e" * 12,
        "biz_id": "no-such",
        "success": True,
    }])
    assert missing.status_code == 500

    sample = _report(client, "sekret-value", [{"out_id": "console-sample", "success": True}])
    assert sample.status_code == 200
    assert sample.json()["msg"] == "接收成功"


def _admin(client):
    body = login(client)
    with psycopg.connect(config.PG_DSN) as conn:
        conn.execute(
            "UPDATE app_user SET role = 'admin' WHERE user_id = %s",
            (body["user"]["user_id"],),
        )
        conn.commit()
    return body


def test_admin_sms_list(client):
    body = _admin(client)
    phone = body["_phone"]
    headers = bearer(body["access_token"])
    listed = client.get("/admin/sms", headers=headers)
    assert listed.status_code == 200
    payload = listed.json()
    assert payload["page"] == 1
    assert "phone_hash" not in str(payload)
    assert payload["phone_mask"] == ""

    queried = client.get("/admin/sms", headers=headers, params={"phone": phone})
    assert queried.status_code == 200
    assert queried.json()["phone_mask"] == phone_mask(phone)
    assert queried.json()["items"]

    bad = client.get("/admin/sms", headers=headers, params={"phone": "123"})
    assert bad.status_code == 422
    assert bad.json()["detail"]["code"] == "invalid_phone"

    wide = client.get(
        "/admin/sms",
        headers=headers,
        params={"from": "2020-01-01", "to": "2020-03-01"},
    )
    assert wide.status_code == 400


def test_stream_requires_redis_but_sync_does_not(client, monkeypatch):
    @contextmanager
    def _no_trace(*_args, **_kwargs):
        yield None

    monkeypatch.setattr("chatbus.open_bus", lambda: None)
    monkeypatch.setattr("observability.tracing.trace", _no_trace)

    class Agent:
        def invoke(self, state):
            return {"answer": "同步还在", "session_id": None}

    monkeypatch.setattr("graph.workflow.get_agent", lambda: Agent())
    denied = client.post("/chat/stream", json={"question": "黑豆怎么吃"})
    assert denied.status_code == 503
    assert "text/event-stream" not in denied.headers.get("content-type", "")
    synced = client.post("/chat", json={"question": "黑豆怎么吃"})
    assert synced.status_code == 200
    assert synced.json()["answer"] == "同步还在"
