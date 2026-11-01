"""根 span 是 node，llm 和 tool 的父节点指向它。"""
from __future__ import annotations

import uuid

import db
from llm.usage import bind_identity, record, set_rid


def test_chat_span_tree(schema, monkeypatch):
    monkeypatch.setattr("config.TRACE_ENABLED", True)
    rid = "span" + uuid.uuid4().hex[:4]
    set_rid(rid)
    bind_identity("u", "sess-span")

    class Agent:
        def invoke(self, state):
            record(kind="tool", node="web_answer", input=state["question"], output="used=True n=1")
            record(kind="llm", model="m", input="prompt", output="回答", prompt_tokens=4, completion_tokens=6, purpose_name="answer")
            return {"answer": "回答", "_graph_hit": False, "session_id": "sess-span"}

    monkeypatch.setattr("graph.workflow.get_agent", lambda: Agent())
    from api.chat import _invoke

    _invoke({"question": "虾能吃吗", "user_id": "u"}, "虾能吃吗", "u")
    with db.connect() as conn:
        rows = conn.execute(
            "SELECT span_id, parent_span_id, kind, node FROM llm_call_log WHERE rid = %s",
            (rid,),
        ).fetchall()
    root = [row for row in rows if row[2] == "node"]
    assert len(root) == 1
    assert root[0][3] == "chat"
    assert root[0][1] == ""
    kids = [row for row in rows if row[2] != "node"]
    assert kids
    assert all(row[1] == root[0][0] for row in kids)
    assert any(row[3] == "web_answer" for row in kids)
    assert not any(row[3] == "assemble" for row in rows)
