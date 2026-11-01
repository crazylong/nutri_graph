"""第 N+1 次 429，被拒不加次数，管理员不计数，匿名领号有上限。"""
from __future__ import annotations

import uuid

import db
from api.deps import get_principal
from app import app


def _agent():
    class Agent:
        def invoke(self, state):
            return {"answer": "好", "session_id": None, "_graph_hit": False}
    return Agent()


def _as(user_id, role="user"):
    app.dependency_overrides[get_principal] = lambda: {
        "kind": "user",
        "user_id": user_id,
        "role": role,
    }


def test_quota_rejects_without_consuming(client, monkeypatch):
    monkeypatch.setattr("config.CHAT_QUOTA_ENABLED", True)
    monkeypatch.setattr("config.CHAT_QUOTA_PER_USER_DAY", 2)
    monkeypatch.setattr("graph.workflow.get_agent", lambda: _agent())
    user_id = "quota-" + uuid.uuid4().hex[:8]
    _as(user_id)
    try:
        assert client.post("/chat", json={"question": "一"}).status_code == 200
        assert client.post("/chat", json={"question": "二"}).status_code == 200
        third = client.post("/chat", json={"question": "三"})
        fourth = client.post("/chat", json={"question": "四"})
    finally:
        app.dependency_overrides.pop(get_principal, None)
    assert third.status_code == 429
    assert third.json()["detail"]["code"] == "quota_exceeded"
    assert fourth.status_code == 429
    with db.connect() as conn:
        n = conn.execute(
            "SELECT n FROM chat_usage_day WHERE user_key = %s",
            (user_id,),
        ).fetchone()[0]
    assert n == 2


def test_two_users_and_admin(client, monkeypatch):
    monkeypatch.setattr("config.CHAT_QUOTA_ENABLED", True)
    monkeypatch.setattr("config.CHAT_QUOTA_PER_USER_DAY", 1)
    monkeypatch.setattr("graph.workflow.get_agent", lambda: _agent())
    a = "qa-" + uuid.uuid4().hex[:6]
    b = "qb-" + uuid.uuid4().hex[:6]
    try:
        _as(a)
        assert client.post("/chat", json={"question": "a1"}).status_code == 200
        assert client.post("/chat", json={"question": "a2"}).status_code == 429
        _as(b)
        assert client.post("/chat", json={"question": "b1"}).status_code == 200
        _as("admin-" + a, role="admin")
        assert client.post("/chat", json={"question": "管理"}).status_code == 200
        assert client.post("/chat", json={"question": "还是管理"}).status_code == 200
    finally:
        app.dependency_overrides.pop(get_principal, None)


def test_anon_issue_cap(client, monkeypatch):
    monkeypatch.setattr("config.ANON_ISSUE_PER_IP_DAY", 2)
    ip = "203.0.113." + str(int(uuid.uuid4().hex[:2], 16) % 200 + 1)
    headers = {"X-Real-IP": ip}
    assert client.post("/auth/anon", headers=headers).status_code == 200
    assert client.post("/auth/anon", headers=headers).status_code == 200
    denied = client.post("/auth/anon", headers=headers)
    assert denied.status_code == 429
    assert denied.json()["detail"]["code"] == "anon_limit_exceeded"
