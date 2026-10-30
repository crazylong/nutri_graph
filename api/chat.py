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

router = APIRouter(tags=["chat"])
log = logging.getLogger("api.chat")


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
    if session_id is not None:
        from memory.short_term import ShortTermMemory

        if not ShortTermMemory().owns(user_id, session_id):
            raise HTTPException(
                status_code=404,
                detail={"code": "session_not_found", "message": "会话不存在"},
            )
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
    from observability.tracing import record_result, redact_trace, trace

    t0 = time.time()
    with trace(question, user_id) as tr:
        result = get_agent().invoke(state)
        elapsed_ms = (time.time() - t0) * 1000
        if tr is not None:
            record_result(
                tr,
                answer=result.get("answer", ""),
                evidence_counts=result.get("_evidence_counts", {}),
                graph_hit=result.get("_graph_hit", False),
                degraded=result.get("_degraded", False),
                elapsed_ms=elapsed_ms,
                web_used=bool((result.get("_web_answer") or {}).get("used")),
            )
        if tr is not None and result.get("_redact"):
            from kg.sensitive import REDACT

            redact_trace(
                tr,
                REDACT,
                keep_output=result.get("_redacted_kind") == "guard",
            )
    return result


@router.post("/chat")
def chat(payload: dict, who: dict | None = Depends(get_principal)):
    """走 LangGraph 检索链路：guard → link → route → graph/vector → assemble → generate。"""
    prepared, error = _prepare(payload, who)
    if error is not None:
        return error
    state, question, user_id, session_id = prepared
    result = _invoke(state, question, user_id)
    return chat_payload(result, session_id)


def _run_stream(bus, stream_id: str, state: dict, question: str, user_id: str, session_id):
    from graph.live import Live
    from llm.streamctx import ModelStreamBroken, StreamCancelled, bind

    live = Live(
        lambda event, data: bus.push(stream_id, event, data),
        cancelled=lambda: bus.cancelled(stream_id),
    )

    def work():
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
            if not live.done:
                live.error("生成中断")
                live.finish_stream()
        except Exception as exc:  # noqa: BLE001
            log.exception("流式生成失败：%s", exc)
            if not live.done:
                live.error("生成失败")
                live.finish_stream()
        if not live.done:
            ok = bus.push(stream_id, "error", {"text": "生成失败"}) and bus.push(
                stream_id, "done", {},
            )
            if not ok and not live.ever_ok:
                bus.delete(stream_id)

    thread = threading.Thread(target=work, name=f"chat-{stream_id[:8]}", daemon=True)
    thread.start()
    return thread


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
    stream_id = uuid.uuid4().hex
    _run_stream(bus, stream_id, state, question, user_id, session_id)
    deadline = time.monotonic() + stream_limit_seconds()
    return StreamingResponse(
        iter_sse(bus, stream_id, deadline),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "X-Accel-Buffering": "no",
        },
    )
