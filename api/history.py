"""
api/history.py —— 历史会话与最近问法

会话和消息已经在 PostgreSQL 里。这里只按当前用户把它们列出来。
最近问法来自 message.role = 'user'，不另建搜索词表。
回答当时的子图存在 message.meta。更早的回答没有这份数据，打开时补查一次并写回。
"""
from __future__ import annotations

import logging

from fastapi import APIRouter, Depends, HTTPException

from api.deps import get_principal, require_user_when_enforced

log = logging.getLogger("api.history")

router = APIRouter(tags=["history"])


def _user_id(who: dict | None) -> str:
    who = require_user_when_enforced(who)
    if who is None:
        raise HTTPException(
            status_code=401,
            detail={"code": "unauthorized", "message": "请先登录"},
        )
    return who["user_id"]


@router.get("/sessions")
def list_sessions(who: dict | None = Depends(get_principal)):
    """当前用户的会话。最多 50 条，按最后一条消息时间倒序。"""
    from memory.short_term import ShortTermMemory

    user_id = _user_id(who)
    return {"sessions": ShortTermMemory().list_sessions(user_id)}


@router.get("/sessions/{session_id}/messages")
def list_messages(session_id: str, who: dict | None = Depends(get_principal)):
    """某次对话的全部消息，按时间正序。不是本人的会话当不存在。"""
    from memory.short_term import ShortTermMemory

    user_id = _user_id(who)
    memory = ShortTermMemory()
    rows = memory.list_messages(user_id, session_id)
    if rows is None:
        raise HTTPException(
            status_code=404,
            detail={"code": "session_not_found", "message": "会话不存在"},
        )
    return {"session_id": session_id, "messages": _fill_missing_cards(memory, user_id, session_id, rows)}


def _fill_missing_cards(memory, user_id: str, session_id: str, rows: list[dict]) -> list[dict]:
    """旧回答的 meta 是空的。按它前面那句用户问题补一次检索，不再调生成模型。"""
    question = ""
    filled = []
    for row in rows:
        if row.get("role") == "user":
            question = row.get("content") or ""
            filled.append(row)
            continue
        if row.get("role") != "assistant" or "graph_hit" in row or not question:
            filled.append(row)
            continue
        try:
            from graph.nodes import retrieve_card

            card = retrieve_card(question, user_id)
        except Exception as exc:  # noqa: BLE001 —— 补图失败仍回放文字
            log.warning("历史回答补图失败：%s", exc)
            filled.append(row)
            continue
        if row.get("id") is not None:
            memory.save_assistant_card(user_id, session_id, row["id"], card)
        filled.append({**row, **card})
    return filled


@router.get("/recent-questions")
def recent_questions(who: dict | None = Depends(get_principal)):
    """最近问过的原话。前端点一下是再发一句，不是打开当时的会话。"""
    from memory.short_term import ShortTermMemory

    user_id = _user_id(who)
    return {"questions": ShortTermMemory().recent_questions(user_id)}
