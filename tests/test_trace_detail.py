"""详情页：一棵树，会话列表不带入参。最近列表按时间倒序，可按手机号收窄。"""
from __future__ import annotations

import uuid

import db
from llm.usage import bind_identity, record, set_rid, span
from security.crypto import phone_mask
from tests.helpers import bearer, login, login_console, rand_phone


def test_trace_tree_and_session_summary(client, monkeypatch):
    monkeypatch.setattr("config.TRACE_ENABLED", True)
    tokens = login_console(client)
    user_id = tokens["user"]["user_id"]
    headers = bearer(tokens["access_token"])
    rid = uuid.uuid4().hex[:8]
    session_id = "sess-" + rid
    set_rid(rid)
    bind_identity("u", session_id)
    with span("node", "chat", input="秘密问句"):
        record(kind="llm", model="m", input="秘密问句", output="答", purpose_name="answer")
    detail = client.get("/admin/trace", params={"rid": rid}, headers=headers)
    assert detail.status_code == 200, detail.text
    spans = detail.json()["spans"]
    assert spans[0]["kind"] == "node"
    assert spans[0]["children"]
    orphan_rid = uuid.uuid4().hex[:8]
    with db.connect() as conn:
        conn.execute(
            "INSERT INTO llm_call_log (rid, span_id, parent_span_id, kind, node, user_id) "
            "VALUES (%s, %s, %s, 'llm', 'answer', %s)",
            (orphan_rid, "childspan01", "missingparent1", user_id),
        )
    orphan = client.get("/admin/trace", params={"rid": orphan_rid}, headers=headers)
    assert orphan.status_code == 200, orphan.text
    assert orphan.json()["spans"][0]["node"] == "answer"
    listing = client.get("/admin/trace/sessions", params={"session_id": session_id}, headers=headers)
    assert listing.status_code == 200
    turn = listing.json()["turns"][0]
    assert turn["rid"] == rid
    assert "秘密" not in str(turn)


def _stamp(rid: str, seconds: int) -> None:
    with db.connect() as conn:
        conn.execute(
            "UPDATE llm_call_log SET ts = now() - make_interval(secs => %s) WHERE rid = %s",
            (seconds, rid),
        )


def test_recent_traces_newest_first_and_by_phone(client, monkeypatch):
    monkeypatch.setattr("config.TRACE_ENABLED", True)
    owner = login_console(client)
    other = login(client)
    owner_id = owner["user"]["user_id"]
    headers = bearer(owner["access_token"])

    old_rid = uuid.uuid4().hex[:8]
    new_rid = uuid.uuid4().hex[:8]
    other_rid = uuid.uuid4().hex[:8]
    set_rid(old_rid)
    bind_identity(owner_id, "sess-old")
    record(kind="llm", model="m", input="旧秘密", output="旧答", purpose_name="answer")
    set_rid(new_rid)
    bind_identity(owner_id, "sess-new")
    record(kind="llm", model="m", input="新秘密", output="新答", purpose_name="answer")
    set_rid(other_rid)
    bind_identity(other["user"]["user_id"], "sess-other")
    record(kind="llm", model="m", input="别人的秘密", output="别人答", purpose_name="answer")
    _stamp(old_rid, 2)
    _stamp(new_rid, 1)
    _stamp(other_rid, 0)

    listing = client.get("/admin/trace/recent", headers=headers)
    assert listing.status_code == 200, listing.text
    body = listing.json()
    assert body["phone_mask"] == ""
    ours = [item["rid"] for item in body["turns"] if item["rid"] in {old_rid, new_rid, other_rid}]
    assert ours == [other_rid, new_rid, old_rid]
    assert "秘密" not in str(body)
    mine = next(item for item in body["turns"] if item["rid"] == new_rid)
    assert mine["phone_mask"] == phone_mask(owner["_phone"])
    assert mine["session_id"] == "sess-new"
    times = [item["latest_at"] for item in body["turns"]]
    assert times == sorted(times, reverse=True)

    queried = client.get("/admin/trace/recent", params={"phone": owner["_phone"]}, headers=headers)
    assert queried.status_code == 200, queried.text
    found = queried.json()
    assert found["phone_mask"] == phone_mask(owner["_phone"])
    assert [item["rid"] for item in found["turns"]] == [new_rid, old_rid]
    assert "秘密" not in str(found)

    unknown = rand_phone()
    missing = client.get("/admin/trace/recent", params={"phone": unknown}, headers=headers)
    assert missing.status_code == 200, missing.text
    assert missing.json()["turns"] == []
    assert missing.json()["phone_mask"] == phone_mask(unknown)

    bad = client.get("/admin/trace/recent", params={"phone": "123"}, headers=headers)
    assert bad.status_code == 422
    assert bad.json()["detail"]["code"] == "invalid_phone"
