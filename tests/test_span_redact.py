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
