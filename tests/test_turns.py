"""
tests/test_turns.py —— 多轮压缩（需求 4）+ 会话内检索（需求 5 的后端部分）

锁的是三条最容易做错的规则：

  1. 轮次：一轮的消息一次性写入、共享同一个 turn_no（按"条"取会在轮中间切开）
  2. 三段读取：最近 N 轮原文 / 水位线已盖住用摘要 / **水位线未盖住仍用原文**
     —— 少了第三段，摘要还没写完时最早那几轮会凭空从提示词里消失
  3. 摘要合并写回：新摘要必须并进旧摘要，覆盖式写入会让长会话只剩最后一段
"""
from __future__ import annotations

import uuid

import pytest

import db

from tests.helpers import PG_OK, requires_pg

pytestmark = requires_pg


def _new_session(user_id: str = "u-turns") -> str:
    from memory.short_term import ShortTermMemory

    return ShortTermMemory().create_session(user_id)


def _rounds(sid: str, n: int) -> None:
    from memory.short_term import ShortTermMemory

    stm = ShortTermMemory()
    for i in range(1, n + 1):
        stm.save_round(sid, f"第{i}轮的问题", f"第{i}轮的回答")


# ---------------------------------------------------------------
# 轮次
# ---------------------------------------------------------------
def test_save_round_writes_one_turn_no_per_round():
    sid = _new_session()
    _rounds(sid, 3)
    with db.connect() as conn:
        rows = conn.execute(
            "SELECT turn_no, role, content FROM message WHERE session_id = %s "
            "ORDER BY turn_no, id",
            (sid,),
        ).fetchall()
    turns = {}
    for turn_no, role, content in rows:
        turns.setdefault(turn_no, []).append((role, content))
    assert sorted(turns) == [1, 2, 3], turns
    # 一轮两条，且顺序是 user → assistant
    for t, msgs in turns.items():
        assert [m[0] for m in msgs] == ["user", "assistant"], msgs
    assert turns[1][0][1] == "第1轮的问题"


def test_round_count_reflects_turns_not_messages():
    from memory.short_term import ShortTermMemory

    sid = _new_session()
    _rounds(sid, 4)
    assert ShortTermMemory().round_count(sid) == 4


# ---------------------------------------------------------------
# 三段读取
# ---------------------------------------------------------------
def test_load_context_keeps_recent_rounds_and_uncompressed_older_rounds():
    """摘要还没写时，更早的轮次**仍然要给原文**。

    只拼「摘要 + 最近 3 轮」的话，第 4 轮刚落库那一刻第 1 轮两头都不在 ——
    数据还在库里，却从提示词里消失。
    """
    from memory.short_term import ShortTermMemory

    stm = ShortTermMemory()
    sid = _new_session()
    _rounds(sid, 5)
    ctx = stm.load_context(sid)
    assert "第5轮的问题" in ctx and "第3轮的问题" in ctx, "最近 3 轮必须是原文"
    assert "第1轮的问题" in ctx, "水位线还没盖住，第 1 轮必须仍以原文出现"
    assert "第2轮的问题" in ctx


def test_load_context_uses_summary_once_waterline_covers_it():
    from memory.short_term import ShortTermMemory

    stm = ShortTermMemory()
    sid = _new_session()
    _rounds(sid, 5)
    with db.connect() as conn:
        row = conn.execute(
            "SELECT MAX(id) FROM message WHERE session_id = %s AND turn_no <= 2", (sid,)
        ).fetchone()
        waterline = int(row[0])
        conn.execute(
            "UPDATE session SET summary = %s, summary_until_msg_id = %s WHERE id = %s",
            ("【摘要】前两轮讲过的要点", waterline, sid),
        )
    ctx = stm.load_context(sid)
    assert "【摘要】前两轮讲过的要点" in ctx
    assert "第1轮的问题" not in ctx, "已被摘要覆盖，不该再给原文（会和摘要重复）"
    assert "第5轮的问题" in ctx


def test_waterline_not_advanced_when_summary_fails():
    from memory.short_term import ShortTermMemory

    stm = ShortTermMemory()
    sid = _new_session()
    _rounds(sid, 5)

    def boom(prev, inc):
        raise RuntimeError("模型挂了")

    assert stm.compress_if_needed(sid, summarize_fn=boom) is False
    with db.connect() as conn:
        row = conn.execute(
            "SELECT COALESCE(summary_until_msg_id, 0), COALESCE(summary, '') "
            "FROM session WHERE id = %s",
            (sid,),
        ).fetchone()
    assert int(row[0]) == 0, "失败不能推进水位线，否则那段对话就真的丢了"
    assert row[1] == ""


# ---------------------------------------------------------------
# 摘要合并写回
# ---------------------------------------------------------------
def test_compress_merges_previous_summary_instead_of_overwriting():
    """第二次压缩必须把旧摘要并进去 —— 覆盖式写入会让长会话只剩最后一段增量。"""
    from memory.short_term import ShortTermMemory

    stm = ShortTermMemory()
    sid = _new_session()
    _rounds(sid, 4)
    with db.connect() as conn:
        conn.execute("UPDATE session SET summary = '【旧摘要】第一轮' WHERE id = %s", (sid,))

    def fake_summarize(prev, inc):
        return f"[{prev or '空'}]+[{inc[:20]}]"

    assert stm.compress_if_needed(sid, summarize_fn=fake_summarize) is True
    with db.connect() as conn:
        row = conn.execute("SELECT summary FROM session WHERE id = %s", (sid,)).fetchone()
    summary = row[0]
    assert "【旧摘要】第一轮" in summary, f"旧摘要被覆盖掉了：{summary}"
    assert "第1轮的问题" in summary, "增量也要进去"


def test_compress_does_nothing_when_rounds_within_window():
    from memory.short_term import ShortTermMemory

    stm = ShortTermMemory()
    sid = _new_session()
    _rounds(sid, 3)
    assert stm.compress_if_needed(sid) is False


def test_compress_is_idempotent_when_nothing_new():
    from memory.short_term import ShortTermMemory

    stm = ShortTermMemory()
    sid = _new_session()
    _rounds(sid, 4)
    ok = stm.compress_if_needed(sid, summarize_fn=lambda prev, inc: f"S:{inc[:10]}")
    assert ok is True
    with db.connect() as conn:
        before = conn.execute("SELECT summary FROM session WHERE id = %s", (sid,)).fetchone()[0]
    # 没有新增轮次，再跑一次不该产生第二条摘要
    assert stm.compress_if_needed(sid, summarize_fn=lambda prev, inc: "不该覆盖") is False
    with db.connect() as conn:
        after = conn.execute("SELECT summary FROM session WHERE id = %s", (sid,)).fetchone()[0]
    assert after == before


# ---------------------------------------------------------------
# 会话内检索（需求 5 后端）
# ---------------------------------------------------------------
def test_list_messages_q_filters_user_messages_only():
    from memory.short_term import ShortTermMemory

    stm = ShortTermMemory()
    sid = _new_session()
    stm.save_round(sid, "黑豆和什么一起吃", "可以配黑米")
    stm.save_round(sid, "菠菜含铁多少", "每 100g 约 2.7mg")

    rows = stm.list_messages("u-turns", sid, q="黑豆")
    assert rows and all(r["role"] == "user" for r in rows)
    assert any("黑豆" in r["content"] for r in rows)

    assert stm.list_messages("u-turns", sid, q="不存在的词") == []


def test_list_messages_q_still_checks_ownership():
    from memory.short_term import ShortTermMemory

    sid = _new_session()
    assert ShortTermMemory().list_messages("别人", sid, q="黑豆") is None


def test_messages_endpoint_requires_ownership(client):
    """新参数不能绕过归属校验：不是本人的会话一律 404。"""
    from tests.helpers import bearer, login

    headers = bearer(login(client)["access_token"])
    sid = str(uuid.uuid4())
    r = client.get(f"/sessions/{sid}/messages", params={"q": "黑豆"}, headers=headers)
    assert r.status_code == 404, r.text
