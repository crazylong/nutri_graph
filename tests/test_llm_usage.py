"""账本：成功一笔、失败 ok=false、没包 purpose 的记 unknown、流式只走 on_success。"""
from __future__ import annotations

import uuid

from langchain_core.messages import AIMessage

import db
from llm.middleware import _Plan
from llm.pool import ModelSpec
from llm.usage import bind_identity, record, set_rid


def _rows(rid):
    with db.connect() as conn:
        return conn.execute(
            "SELECT purpose, ok, kind FROM llm_call_log WHERE rid = %s ORDER BY id",
            (rid,),
        ).fetchall()


def test_success_and_unknown_and_failure(schema, monkeypatch):
    monkeypatch.setattr("config.TRACE_ENABLED", True)
    rid = uuid.uuid4().hex[:8]
    set_rid(rid)
    bind_identity("user-a", "sess-1")
    spec = ModelSpec(name="fake-model")
    msg = AIMessage(content="好", usage_metadata={"input_tokens": 2, "output_tokens": 3, "total_tokens": 5})
    plan = _Plan({})
    plan.on_success(spec, msg, msg, prompt="问", latency_ms=10)
    plan.on_starved(spec, "空", msg=msg, prompt="问", latency_ms=4)
    record(kind="llm", model="x", input="裸", output="答")
    rows = _rows(rid)
    assert [row[1] for row in rows[:2]] == [True, False]
    assert rows[0][0] == "unknown"
    assert rows[2][0] == "unknown"
    assert all(row[2] == "llm" for row in rows)


def test_stream_success_is_one_row(schema, monkeypatch):
    """流式成功只调用 on_success 一次，账上就一行。"""
    monkeypatch.setattr("config.TRACE_ENABLED", True)
    rid = uuid.uuid4().hex[:8]
    set_rid(rid)
    bind_identity("user-b", None)
    spec = ModelSpec(name="fake-model")
    msg = AIMessage(content="流", usage_metadata={"input_tokens": 1, "output_tokens": 1, "total_tokens": 2})
    _Plan({}).on_success(spec, msg, msg, prompt="问", latency_ms=8)
    assert len(_rows(rid)) == 1
