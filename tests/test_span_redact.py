"""打码要盖住已经写下的行，也要盖住打码之后才启动的线程。"""
from __future__ import annotations

import uuid

import db
from kg.sensitive import REDACT
from llm.usage import bind_identity, mark_redact, record, redact_stored, set_rid
from utils.spawn import spawn


def _text(rid):
    with db.connect() as conn:
        return conn.execute(
            "SELECT input, output FROM llm_call_log WHERE rid = %s ORDER BY id",
            (rid,),
        ).fetchall()


def test_redact_covers_past_and_future_rows(schema, monkeypatch):
    monkeypatch.setattr("config.TRACE_ENABLED", True)
    rid = uuid.uuid4().hex[:8]
    set_rid(rid)
    bind_identity("u", None)
    record(kind="llm", model="m", input="过敏原句", output="原文回答", purpose_name="answer")
    mark_redact(keep_output=False)

    def later():
        record(kind="llm", model="m", input="线程里的原句", output="线程回答", purpose_name="profile")

    spawn("profile-extract", later).join(2)
    redact_stored(rid, keep_output=False)
    rows = _text(rid)
    blob = " ".join(cell for row in rows for cell in row)
    assert "过敏" not in blob
    assert "原句" not in blob
    assert all(cell == REDACT for row in rows for cell in row)


def test_long_text_is_truncated(schema, monkeypatch):
    monkeypatch.setattr("config.TRACE_ENABLED", True)
    rid = uuid.uuid4().hex[:8]
    set_rid(rid)
    record(kind="llm", model="m", input="啊" * 3000, output="好", purpose_name="answer")
    assert len(_text(rid)[0][0]) == 2048


def _inline_summary(monkeypatch):
    def submit(name, fn, *args):
        assert name == "trace-summary"
        fn(*args)
        return True

    monkeypatch.setattr("utils.spawn.submit_background", submit)


def test_text_limit_is_configurable(schema, monkeypatch):
    monkeypatch.setattr("config.TRACE_ENABLED", True)
    monkeypatch.setattr("config.TRACE_TEXT_MAX", 10)
    monkeypatch.setattr("config.TRACE_SUMMARY_ENABLED", False)
    rid = uuid.uuid4().hex[:8]
    set_rid(rid)
    record(kind="llm", model="m", input="啊" * 30, output="好" * 30, purpose_name="answer")
    saved = _text(rid)[0]
    assert len(saved[0]) == 10
    assert len(saved[1]) == 10


def test_summary_only_when_trace_and_switch_are_on(schema, monkeypatch):
    monkeypatch.setattr("config.TRACE_ENABLED", False)
    monkeypatch.setattr("config.TRACE_SUMMARY_ENABLED", True)
    monkeypatch.setattr("config.TRACE_TEXT_MAX", 10)

    def refuse(*_args, **_kwargs):
        raise AssertionError("追踪关闭时不该生成摘要")

    monkeypatch.setattr("utils.spawn.submit_background", refuse)
    rid = uuid.uuid4().hex[:8]
    set_rid(rid)
    record(kind="llm", model="m", input="啊" * 30, output="好", purpose_name="answer")
    assert _text(rid)[0] == ("", "")


def test_over_limit_writes_summary_without_recursion(schema, monkeypatch):
    monkeypatch.setattr("config.TRACE_ENABLED", True)
    monkeypatch.setattr("config.TRACE_SUMMARY_ENABLED", True)
    monkeypatch.setattr("config.TRACE_TEXT_MAX", 10)
    _inline_summary(monkeypatch)

    def fake_summary(full_in, full_out):
        assert len(full_in) > 10
        assert full_out == ""
        return "入参摘要", "m", 3, 2, 15

    monkeypatch.setattr("llm.usage._make_summary", fake_summary)
    rid = uuid.uuid4().hex[:8]
    set_rid(rid)
    record(kind="llm", model="m", input="啊" * 30, output="短", purpose_name="answer")
    with db.connect() as conn:
        rows = conn.execute(
            "SELECT purpose, input, output, summary FROM llm_call_log WHERE rid = %s ORDER BY id",
            (rid,),
        ).fetchall()
    assert rows[0][0] == "answer"
    assert len(rows[0][1]) == 10
    assert rows[0][2] == "短"
    assert rows[0][3] == "入参摘要"
    assert [row[0] for row in rows].count("trace_summary") == 1
    assert rows[1][1] == ""
    assert rows[1][2] == ""


def test_redact_clears_summary(schema, monkeypatch):
    monkeypatch.setattr("config.TRACE_ENABLED", True)
    rid = uuid.uuid4().hex[:8]
    set_rid(rid)
    record(kind="llm", model="m", input="过敏原句", output="原文回答", purpose_name="answer")
    with db.connect() as conn:
        conn.execute(
            "UPDATE llm_call_log SET summary = %s WHERE rid = %s",
            ("过敏摘要", rid),
        )
    redact_stored(rid, keep_output=False)
    with db.connect() as conn:
        row = conn.execute(
            "SELECT input, output, summary FROM llm_call_log WHERE rid = %s",
            (rid,),
        ).fetchone()
    assert row == (REDACT, REDACT, REDACT)
