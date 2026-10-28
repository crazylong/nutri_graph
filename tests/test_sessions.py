"""会话：不传 id 就新开，传入则续写，别人的 id 当不存在。"""
from __future__ import annotations

from tests.helpers import bearer, login, requires_pg

pytestmark = requires_pg


def _install_agent(monkeypatch):
    """绕开图谱和模型，只走记忆落库。归属校验发生在进图之前。"""

    def invoke(state):
        from graph.nodes import memory_write_node

        state = {**state, "answer": "没有找到确切依据"}
        return {**state, **memory_write_node(state)}

    class Agent:
        def invoke(self, state):
            return invoke(state)

    monkeypatch.setattr("graph.workflow.get_agent", lambda: Agent())


def test_new_session_unless_id_is_sent(client, monkeypatch):
    _install_agent(monkeypatch)
    headers = bearer(login(client)["access_token"])
    long_q = "甲" * 50

    first = client.post("/chat", json={"question": long_q}, headers=headers)
    assert first.status_code == 200, first.text
    sid1 = first.json()["session_id"]

    second = client.post("/chat", json={"question": "乙的另一轮"}, headers=headers)
    assert second.status_code == 200, second.text
    sid2 = second.json()["session_id"]
    assert sid1 != sid2

    resumed = client.post(
        "/chat",
        json={"question": "回到甲", "session_id": sid1},
        headers=headers,
    )
    assert resumed.status_code == 200, resumed.text
    assert resumed.json()["session_id"] == sid1

    listed = client.get("/sessions", headers=headers).json()["sessions"]
    assert listed[0]["id"] == sid1
    titled = next(row for row in listed if row["id"] == sid1)
    assert titled["title"] == "甲" * 40
    assert titled["message_count"] == 4

    messages = client.get(f"/sessions/{sid1}/messages", headers=headers).json()["messages"]
    asked = [row["content"] for row in messages if row["role"] == "user"]
    assert asked == [long_q, "回到甲"]

    recent = client.get("/recent-questions", headers=headers).json()["questions"]
    assert recent[0]["question"] == "回到甲"
    assert recent[0]["session_id"] == sid1
    assert all(row["question"] != "没有找到确切依据" for row in recent)


def test_foreign_or_unknown_session_is_not_found(client, monkeypatch):
    _install_agent(monkeypatch)
    owner = bearer(login(client)["access_token"])
    other = bearer(login(client)["access_token"])
    created = client.post("/chat", json={"question": "只属于我"}, headers=owner)
    sid = created.json()["session_id"]

    denied = client.post(
        "/chat",
        json={"question": "偷看", "session_id": sid},
        headers=other,
    )
    assert denied.status_code == 404

    missing = client.get(f"/sessions/{sid}/messages", headers=other)
    assert missing.status_code == 404

    bad = client.post(
        "/chat",
        json={"question": "坏id", "session_id": "not-a-uuid"},
        headers=other,
    )
    assert bad.status_code == 404

    listed = client.get("/sessions", headers=other).json()["sessions"]
    assert all(row["id"] != sid for row in listed)
