"""没带租户不检索。错误租户是空结果，回答固定弃答句。"""
from __future__ import annotations

from kb.common import ABSTAIN
from tests.helpers import requires_pg

pytestmark = requires_pg


def test_missing_tenant_does_not_search(client, monkeypatch):
    def boom(*args, **kwargs):
        raise AssertionError("没有租户不应该检索")

    monkeypatch.setattr("kb.workflow.search_question", boom)
    monkeypatch.setattr("kb.workflow.search_picture", boom)
    response = client.post("/kb/ask", data={"question": "差旅费怎么报"})
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["abstained"] is True
    assert body["answer"] == ABSTAIN
    assert body["hits"] == []


def test_wrong_tenant_abstains(client, monkeypatch):
    monkeypatch.setattr("kb.workflow.search_question", lambda *args, **kwargs: [])

    def no_model(*args, **kwargs):
        raise AssertionError("没有命中不应该调用模型")

    monkeypatch.setattr("kb.workflow.invoke_model", no_model)
    response = client.post("/kb/ask", data={"tenant_id": "nobody", "question": "差旅费怎么报"})
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["hits"] == []
    assert body["answer"] == ABSTAIN
    assert body["abstained"] is True


def test_search_limit_follows_config(client, monkeypatch):
    import config

    monkeypatch.setattr(config, "KB_SEARCH_LIMIT", 2)
    seen = {}

    def spy(tenant_id, question, limit=None):
        seen["limit"] = limit
        return []

    monkeypatch.setattr("kb.workflow.search_question", spy)
    response = client.post("/kb/ask", data={"tenant_id": "nobody", "question": "差旅费怎么报"})
    assert response.status_code == 200, response.text
    assert seen["limit"] == 2


def test_later_turn_keeps_recent_dialogue_and_image(client, monkeypatch):
    monkeypatch.setattr("kb.memory._defer_compress", lambda session_id: None)
    seen = {}

    def capture(prompt, enable_thinking=False):
        seen["prompt"] = prompt

        class Msg:
            content = "按票据实报。"

        return Msg()

    monkeypatch.setattr("kb.workflow.invoke_model", capture)
    monkeypatch.setattr(
        "kb.workflow.search_question",
        lambda *args, **kwargs: [{
            "chunk_id": "c1",
            "block_kind": "image",
            "text": "报销流程图：先填单再贴票。",
            "heading_path": "制度 / 报销",
            "image_path": "/tmp/flow.png",
            "score": 0.9,
        }],
    )
    first = client.post(
        "/kb/ask",
        data={"tenant_id": "acme", "question": "差旅费怎么报", "client_id": "kbclient01"},
    )
    assert first.status_code == 200, first.text
    body = first.json()
    assert body["session_id"]
    assert body["hits"][0]["text"]
    assert "c1" in body["hits"][0]["image_url"]
    assert "近期对话" not in seen["prompt"]

    second = client.post(
        "/kb/ask",
        data={
            "tenant_id": "acme",
            "question": "那住宿呢",
            "client_id": "kbclient01",
            "session_id": body["session_id"],
        },
    )
    assert second.status_code == 200, second.text
    assert "差旅费怎么报" in seen["prompt"]
    assert "不能当成文档依据" in seen["prompt"]
    saved = client.get("/kb/sessions/" + body["session_id"], params={"client_id": "kbclient01"})
    assert saved.status_code == 200, saved.text
    assert [item["role"] for item in saved.json()["messages"]] == [
        "user", "assistant", "user", "assistant",
    ]
