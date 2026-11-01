"""
api/chat.py —— 主问答入口

身份优先级：已登录用户 > 匿名身份 > payload.user_id
（最后一项只在 AUTH_REQUIRED=false 的开发模式生效；
  AUTH_REQUIRED=true 时伪造 user_id 与匿名身份都会被拒 —— 这正是 needs 13 堵的口子）
"""
from __future__ import annotations

import logging
import threading
import time
import uuid

from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import JSONResponse, StreamingResponse

import config
from api.deps import get_principal, require_user_when_enforced
from api.metrics import note
from utils.clock import seconds_until_midnight

router = APIRouter(tags=["chat"])
log = logging.getLogger("api.chat")
_stream_slots: threading.Semaphore | None = None
_stream_slots_n: int | None = None


def _slots() -> threading.Semaphore:
    global _stream_slots, _stream_slots_n
    n = max(int(config.STREAM_MAX_CONCURRENT), 1)
    if _stream_slots is None or _stream_slots_n != n:
        _stream_slots = threading.Semaphore(n)
        _stream_slots_n = n
    return _stream_slots


def _facts(result: dict) -> list[dict]:
    """图命中行 → 表格行。没走图谱时是空列表，前端据此不渲染表格。"""
    from kg.subgraph import build_facts

    return build_facts(result.get("_graph_results") or [])


def _subgraph(result: dict) -> dict | None:
    """图命中行 → nodes/edges。只在这条回答真的走过图谱时才有值。"""
    from kg.subgraph import build_subgraph

    return build_subgraph(result.get("_entities") or [], result.get("_graph_results") or [])


def chat_payload(result: dict, session_id: str | None) -> dict:
    """同步 /chat 和流式 result 事件共用。不落库时沿用请求里的 session_id。"""
    return {
        "answer": result.get("answer", ""),
        "confidence": result.get("confidence", 0.0),
        "basis": result.get("basis", []),
        "gaps": result.get("gaps", []),
        "degraded": result.get("_degraded", False),
        "graph_hit": result.get("_graph_hit", False),
        "evidence_counts": result.get("_evidence_counts", {}),
        "facts": _facts(result),
        "subgraph": _subgraph(result),
        "food_requests": result.get("_food_requests", []),
        "web_answer": result.get("_web_answer") or {"used": False, "sources": [], "blocked": ""},
        "session_id": result.get("session_id") or session_id,
        "disclaimer": config.DISCLAIMER,
    }


def _prepare(payload: dict, who: dict | None):
    question = payload.get("question", "")
    session_id = (payload.get("session_id") or "").strip() or None
    who = require_user_when_enforced(who)
    user_id = who["user_id"] if who else payload.get("user_id", "anonymous")
    if not question:
        return None, JSONResponse(status_code=400, content={"error": "question 不能为空"})
    from memory.short_term import ShortTermMemory

    memory = ShortTermMemory()
    if session_id is not None:
        if not memory.owns(user_id, session_id):
            raise HTTPException(
                status_code=404,
                detail={"code": "session_not_found", "message": "会话不存在"},
            )
    if config.CHAT_QUOTA_ENABLED:
        if who is None:
            raise HTTPException(
                status_code=401,
                detail={"code": "unauthorized", "message": "请先领取身份后再提问"},
            )
        if who.get("role") != "admin":
            from security.quota import charge_chat

            if not charge_chat(user_id):
                raise HTTPException(
                    status_code=429,
                    detail={"code": "quota_exceeded", "message": "今日提问次数已用完"},
                    headers={"Retry-After": str(seconds_until_midnight())},
                )
    if session_id is None and config.MEMORY_ENABLED:
        try:
            session_id = memory.create_session(user_id)
        except Exception:
            log.warning("开会话失败，这次调用先不带 session", exc_info=True)
    from llm.usage import bind_identity

    bind_identity(user_id, session_id)
    raw_consent = payload.get("consent", None)
    state = {
        "question": question,
        "user_id": user_id,
        "session_id": session_id,
        "consent_profile": True if raw_consent is None else bool(raw_consent),
    }
    return (state, question, user_id, session_id), None


def _invoke(state: dict, question: str, user_id: str) -> dict:
    from graph.workflow import get_agent
    from llm.usage import current_rid, purpose, redact_stored, span

    t0 = time.perf_counter()
    with purpose("answer"):
        with span("node", "chat", input=question) as box:
            result = get_agent().invoke(state)
            box.output = result.get("answer") or ""
    if result.get("_redact"):
        redact_stored(current_rid(), keep_output=result.get("_redacted_kind") == "guard")
    elapsed_ms = int((time.perf_counter() - t0) * 1000)
    if result.get("_abstain"):
        status = "abstain"
    elif result.get("_degraded"):
        status = "degraded"
    else:
        status = "ok"
    note(status, bool(result.get("_graph_hit")), elapsed_ms)
    return result


@router.post("/chat")
def chat(payload: dict, who: dict | None = Depends(get_principal)):
    """走 LangGraph 检索链路：guard → link → route → graph/vector → assemble → generate。"""
    prepared, error = _prepare(payload, who)
    if error is not None:
        return error
    state, question, user_id, session_id = prepared
    try:
        result = _invoke(state, question, user_id)
    except Exception:
        note("error", False, 0)
        raise
    return chat_payload(result, session_id)


def _run_stream(bus, stream_id: str, state: dict, question: str, user_id: str, session_id, *, release_slot: bool = False):
    from graph.live import Live
    from llm.streamctx import ModelStreamBroken, StreamCancelled, bind

    live = Live(
        lambda event, data: bus.push(stream_id, event, data),
        cancelled=lambda: bus.cancelled(stream_id),
    )

    def work():
        try:
            try:
                with bind(live):
                    live.status("正在查找")
                    result = _invoke(state, question, user_id)
                    if not live.sent_text and result.get("answer"):
                        live.text_delta(result["answer"])
                    live.result(chat_payload(result, session_id))
                    live.finish_stream()
            except (StreamCancelled, ModelStreamBroken) as exc:
                log.info("流式中断：%s", exc)
                note("error", False, 0)
                if not live.done:
                    live.error("生成中断")
                    live.finish_stream()
            except Exception as exc:  # noqa: BLE001
                log.exception("流式生成失败：%s", exc)
                note("error", False, 0)
                if not live.done:
                    live.error("生成失败")
                    live.finish_stream()
            if not live.done:
                ok = bus.push(stream_id, "error", {"text": "生成失败"}) and bus.push(
                    stream_id, "done", {},
                )
                if not ok and not live.ever_ok:
                    bus.delete(stream_id)
        finally:
            if release_slot:
                _slots().release()

    from utils.spawn import spawn

    return spawn(f"chat-{stream_id[:8]}", work)


@router.post("/chat/stream")
def chat_stream(payload: dict, who: dict | None = Depends(get_principal)):
    """与 /chat 相同的图。事件放 Redis 列表，这条连接只负责往外读。"""
    prepared, error = _prepare(payload, who)
    if error is not None:
        return error
    from chatbus import iter_sse, open_bus, stream_limit_seconds

    bus = open_bus()
    if bus is None:
        return JSONResponse(status_code=503, content={"error": "流式通道不可用"})
    state, question, user_id, session_id = prepared
    slots = _slots()
    if not slots.acquire(blocking=False):
        return JSONResponse(
            status_code=503,
            content={"detail": {"code": "stream_busy", "message": "当前回答过多，请稍后再试"}},
        )
    stream_id = uuid.uuid4().hex
    try:
        _run_stream(bus, stream_id, state, question, user_id, session_id, release_slot=True)
    except Exception:
        slots.release()
        raise
    deadline = time.monotonic() + stream_limit_seconds()
    return StreamingResponse(
        iter_sse(bus, stream_id, deadline),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "X-Accel-Buffering": "no",
        },
    )
