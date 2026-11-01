"""费用统计不把根 span 和 tool span 算进调用次数。"""
from __future__ import annotations

import uuid

from api.admin import _usage_today
from llm.usage import bind_identity, record, set_rid, span


def test_stats_count_only_llm_kind(schema, monkeypatch):
    monkeypatch.setattr("config.TRACE_ENABLED", True)
    rid = uuid.uuid4().hex[:8]
    set_rid(rid)
    bind_identity("cost-user", None)
    with span("node", "chat", input="问"):
        record(kind="tool", node="link", input="问", output="1")
        record(kind="llm", model="m", input="问", output="答", prompt_tokens=10, completion_tokens=2, purpose_name="answer")
    import db

    with db.connect() as conn:
        usage = _usage_today(conn)
    assert usage["today_calls"] >= 1
    with db.connect() as conn:
        n = conn.execute(
            "SELECT count(*) FROM llm_call_log WHERE rid = %s AND kind = 'llm'",
            (rid,),
        ).fetchone()[0]
        calls_for_rid = conn.execute(
            """
            SELECT count(*) FROM llm_call_log
            WHERE rid = %s AND kind = 'llm'
            """,
            (rid,),
        ).fetchone()[0]
    assert n == 1
    assert calls_for_rid == 1
    assert usage["today_tokens"] >= 12
