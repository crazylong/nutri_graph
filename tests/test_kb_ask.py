"""没带租户不检索。错误租户是空结果，回答固定弃答句。"""
from __future__ import annotations

import json
import threading
import time

from kb.common import ABSTAIN
from tests.helpers import bearer, kb_staff, requires_pg

pytestmark = requires_pg


def _who(client, tenant="acme"):
    return bearer(kb_staff(client, tenant)["access_token"])


def test_missing_tenant_does_not_search(client, monkeypatch):
    def boom(*args, **kwargs):
        raise AssertionError("没有租户不应该检索")

    monkeypatch.setattr("kb.workflow.search_question", boom)
    monkeypatch.setattr("kb.workflow.search_picture", boom)
    response = client.post("/kb/ask", data={"question": "差旅费怎么报"}, headers=_who(client))
    assert response.status_code == 422, response.text


def test_wrong_tenant_abstains(client, monkeypatch):
    monkeypatch.setattr("kb.workflow.search_question", lambda *args, **kwargs: [])

    def no_model(*args, **kwargs):
        raise AssertionError("没有命中不应该调用模型")

    monkeypatch.setattr("kb.workflow.invoke_model", no_model)
    response = client.post(
        "/kb/ask",
        data={"tenant_id": "nobody", "question": "差旅费怎么报"},
        headers=_who(client, "nobody"),
    )
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["hits"] == []
    assert body["answer"] == ABSTAIN
    assert body["abstained"] is True


def test_search_limit_follows_config(client, monkeypatch):
    import config

    monkeypatch.setattr(config, "KB_SEARCH_LIMIT", 2)
    seen = {}
    rows = []
    for i in range(10):
        base = 0x4e00 + i * 20
        rows.append({
            "chunk_id": str(i),
            "block_kind": "text",
            "text": "".join(chr(base + k) for k in range(12)),
            "heading_path": "",
            "image_path": "",
            "score": 1,
        })

    def spy(tenant_id, question, limit=None):
        seen["limit"] = limit
        return rows

    class _Msg:
        content = "按票据实报。"

    monkeypatch.setattr("kb.workflow.search_question", spy)
    monkeypatch.setattr("kb.workflow.invoke_model", lambda *args, **kwargs: _Msg())
    response = client.post(
        "/kb/ask",
        data={"tenant_id": "nobody", "question": "差旅费怎么报"},
        headers=_who(client, "nobody"),
    )
    assert response.status_code == 200, response.text
    assert seen["limit"] == max(config.KB_RERANK_CANDIDATES, 2)
    assert [hit["chunk_id"] for hit in response.json()["hits"]] == ["0", "1"]


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
    headers = _who(client)
    first = client.post(
        "/kb/ask",
        data={"tenant_id": "acme", "question": "差旅费怎么报"},
        headers=headers,
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
            "session_id": body["session_id"],
        },
        headers=headers,
    )
    assert second.status_code == 200, second.text
    assert "差旅费怎么报" in seen["prompt"]
    assert "不能当成文档依据" in seen["prompt"]
    saved = client.get("/kb/sessions/" + body["session_id"], params={"tenant_id": "acme"}, headers=headers)
    assert saved.status_code == 200, saved.text
    assert [item["role"] for item in saved.json()["messages"]] == [
        "user", "assistant", "user", "assistant",
    ]


_HIT = {
    "chunk_id": "c1",
    "block_kind": "text",
    "text": "加班按小时计。",
    "heading_path": "制度 / 考勤",
    "score": 0.8,
}


def _quiet_memory(monkeypatch):
    monkeypatch.setattr("kb.memory._defer_compress", lambda session_id: None)


def _hits(monkeypatch):
    monkeypatch.setattr("kb.workflow.search_question", lambda *args, **kwargs: [dict(_HIT)])


def _ask(client, headers, question, session_id=""):
    data = {"tenant_id": "acme", "question": question}
    if session_id:
        data["session_id"] = session_id
    return client.post("/kb/ask", data=data, headers=headers)


def _sse_events(body: str):
    found = []
    for frame in body.split("\n\n"):
        if not frame or frame.startswith(":"):
            continue
        event = "message"
        data_lines = []
        for line in frame.split("\n"):
            if line.startswith("event:"):
                event = line[6:].strip()
            elif line.startswith("data:"):
                data_lines.append(line[5:].strip())
        if data_lines:
            found.append((event, json.loads("".join(data_lines))))
    return found


def test_session_list_orders_by_last_active_and_rejects_bad_client(client, monkeypatch):
    _quiet_memory(monkeypatch)
    _hits(monkeypatch)
    monkeypatch.setattr("kb.workflow.invoke_model", lambda prompt, enable_thinking=False: type("M", (), {"content": "有据。"})())
    headers = _who(client)
    first = _ask(client, headers, "第一问考勤制度")
    assert first.status_code == 200, first.text
    second = _ask(client, headers, "第二问加班费")
    assert second.status_code == 200, second.text
    listed = client.get("/kb/sessions", params={"tenant_id": "acme"}, headers=headers)
    assert listed.status_code == 200, listed.text
    rows = listed.json()["sessions"]
    assert [row["id"] for row in rows[:2]] == [second.json()["session_id"], first.json()["session_id"]]
    assert rows[0]["title"] == "第二问加班费"
    bad = client.get("/kb/sessions", params={"tenant_id": "acme"})
    assert bad.status_code == 401, bad.text


def test_recent_questions_are_newest_first(client, monkeypatch):
    _quiet_memory(monkeypatch)
    _hits(monkeypatch)
    monkeypatch.setattr("kb.workflow.invoke_model", lambda prompt, enable_thinking=False: type("M", (), {"content": "有据。"})())
    headers = _who(client)
    _ask(client, headers, "先问差旅")
    _ask(client, headers, "再问住宿")
    listed = client.get("/kb/recent", params={"tenant_id": "acme"}, headers=headers)
    assert listed.status_code == 200, listed.text
    questions = [row["question"] for row in listed.json()["questions"]]
    assert questions[:2] == ["再问住宿", "先问差旅"]


def test_sessions_stay_inside_one_client(client, monkeypatch):
    _quiet_memory(monkeypatch)
    _hits(monkeypatch)
    monkeypatch.setattr("kb.workflow.invoke_model", lambda prompt, enable_thinking=False: type("M", (), {"content": "有据。"})())
    left_headers = _who(client)
    right_headers = _who(client)
    _ask(client, left_headers, "甲的问题")
    _ask(client, right_headers, "乙的问题")
    left = client.get("/kb/sessions", params={"tenant_id": "acme"}, headers=left_headers)
    right = client.get("/kb/sessions", params={"tenant_id": "acme"}, headers=right_headers)
    left_titles = [row["title"] for row in left.json()["sessions"]]
    right_titles = [row["title"] for row in right.json()["sessions"]]
    assert "甲的问题" in left_titles
    assert "乙的问题" not in left_titles
    assert "乙的问题" in right_titles
    assert "甲的问题" not in right_titles


def test_streamer_flushes_unpunctuated_tail():
    from kb.streamer import KbStreamer

    events = []
    clock = {"now": 0.0}
    streamer = KbStreamer(
        lambda event, data: events.append((event, data)) or True,
        clock=lambda: clock["now"],
    )
    assert streamer.feed("没有句号") is True
    assert [event for event, _data in events] == []
    streamer.flush()
    assert "".join(data["text"] for event, data in events if event == "text_delta") == "没有句号"
    assert streamer.sent_text is True
    streamer.flush()
    assert [data["text"] for event, data in events if event == "text_delta"] == ["没有句号"]


def _stream_bus(monkeypatch):
    from chatbus import MemoryBus

    bus = MemoryBus()
    monkeypatch.setattr("chatbus.open_bus", lambda: bus)
    return bus


def test_stream_emits_deltas_and_saves_hits(client, monkeypatch):
    _quiet_memory(monkeypatch)
    _hits(monkeypatch)
    _stream_bus(monkeypatch)
    seen = {}

    def fake_stream(prompt, enable_thinking=False):
        seen["prompt"] = prompt
        return "没有句号的回答"

    monkeypatch.setattr("kb.workflow.stream_model", fake_stream)
    headers = _who(client)
    first = client.post(
        "/kb/ask/stream",
        data={"tenant_id": "acme", "question": "考勤怎么算"},
        headers=headers,
    )
    assert first.status_code == 200, first.text
    assert "text/event-stream" in (first.headers.get("content-type") or "")
    frames = _sse_events(first.text)
    names = [event for event, _data in frames]
    assert "text_delta" in names
    assert "result" in names
    text = "".join(data.get("text") or "" for event, data in frames if event == "text_delta")
    assert text == "没有句号的回答"
    result = next(data for event, data in frames if event == "result")
    assert result["hits"][0]["chunk_id"] == "c1"
    assert result["session_id"]
    assert "近期对话" not in seen["prompt"]

    second = client.post(
        "/kb/ask/stream",
        data={
            "tenant_id": "acme",
            "question": "那加班呢",
            "session_id": result["session_id"],
        },
        headers=headers,
    )
    assert second.status_code == 200, second.text
    assert "考勤怎么算" in seen["prompt"]
    saved = client.get("/kb/sessions/" + result["session_id"], params={"tenant_id": "acme"}, headers=headers)
    assert saved.status_code == 200, saved.text
    messages = saved.json()["messages"]
    assert [item["role"] for item in messages] == ["user", "assistant", "user", "assistant"]
    assert messages[1]["hits"][0]["text"] == "加班按小时计。"
    listed = client.get("/kb/sessions", params={"tenant_id": "acme"}, headers=headers)
    assert listed.json()["sessions"][0]["id"] == result["session_id"]


def test_stream_cancel_does_not_save_messages(client, monkeypatch):
    _quiet_memory(monkeypatch)
    _hits(monkeypatch)
    bus = _stream_bus(monkeypatch)

    def fake_stream(prompt, enable_thinking=False):
        from llm.streamctx import StreamCancelled, hook

        target = hook()

        def cancel_soon():
            time.sleep(0.05)
            for key in list(bus.lists):
                bus.cancel(key.rsplit(":", 1)[-1])

        threading.Thread(target=cancel_soon, daemon=True).start()
        for _ in range(400):
            if target is not None and target.cancelled():
                raise StreamCancelled()
            time.sleep(0.01)
        raise AssertionError("取消没有传到生成线程")

    monkeypatch.setattr("kb.workflow.stream_model", fake_stream)
    headers = _who(client)
    response = client.post(
        "/kb/ask/stream",
        data={"tenant_id": "acme", "question": "这轮不该落库"},
        headers=headers,
    )
    assert response.status_code == 200, response.text
    names = [event for event, _data in _sse_events(response.text)]
    assert "error" in names
    assert "生成中断" in response.text
    assert "done" in names
    listed = client.get("/kb/sessions", params={"tenant_id": "acme"}, headers=headers)
    assert listed.status_code == 200, listed.text
    rows = listed.json()["sessions"]
    assert rows
    for row in rows:
        saved = client.get("/kb/sessions/" + row["id"], params={"tenant_id": "acme"}, headers=headers)
        assert saved.status_code == 200, saved.text
        assert saved.json()["messages"] == []


def test_stream_model_failure_finishes_and_releases_slot(client, monkeypatch):
    import config
    from llm import LLMUnavailableError

    _quiet_memory(monkeypatch)
    _hits(monkeypatch)
    _stream_bus(monkeypatch)
    monkeypatch.setattr(config, "STREAM_MAX_CONCURRENT", 1)

    def boom(prompt, enable_thinking=False):
        raise LLMUnavailableError("池空了")

    monkeypatch.setattr("kb.workflow.stream_model", boom)
    headers = _who(client)
    failed = client.post(
        "/kb/ask/stream",
        data={"tenant_id": "acme", "question": "模型挂了"},
        headers=headers,
    )
    assert failed.status_code == 200, failed.text
    names = [event for event, _data in _sse_events(failed.text)]
    assert "error" in names
    assert names[-1] == "done"

    def ok(prompt, enable_thinking=False):
        return "恢复了"

    monkeypatch.setattr("kb.workflow.stream_model", ok)
    again = client.post(
        "/kb/ask/stream",
        data={"tenant_id": "acme", "question": "再试一次"},
        headers=headers,
    )
    assert again.status_code == 200, again.text
    assert "text/event-stream" in (again.headers.get("content-type") or "")
    text = "".join(data.get("text") or "" for event, data in _sse_events(again.text) if event == "text_delta")
    assert text == "恢复了"
