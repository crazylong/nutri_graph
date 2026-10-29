"""
api/chat.py —— 主问答入口

身份优先级：已登录用户 > 匿名身份 > payload.user_id
（最后一项只在 AUTH_REQUIRED=false 的开发模式生效；
  AUTH_REQUIRED=true 时伪造 user_id 与匿名身份都会被拒 —— 这正是 needs 13 堵的口子）
"""
from __future__ import annotations

import time

from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import JSONResponse

import config
from api.deps import get_principal, require_user_when_enforced

router = APIRouter(tags=["chat"])


def _facts(result: dict) -> list[dict]:
    """图命中行 → 表格行。没走图谱时是空列表，前端据此不渲染表格。"""
    from kg.subgraph import build_facts

    return build_facts(result.get("_graph_results") or [])


def _subgraph(result: dict) -> dict | None:
    """图命中行 → nodes/edges。只在这条回答真的走过图谱时才有值。"""
    from kg.subgraph import build_subgraph

    return build_subgraph(result.get("_entities") or [], result.get("_graph_results") or [])


@router.post("/chat")
def chat(payload: dict, who: dict | None = Depends(get_principal)):
    """走 LangGraph 检索链路：guard → link → route → graph/vector → assemble → generate。"""
    question = payload.get("question", "")
    session_id = (payload.get("session_id") or "").strip() or None

    who = require_user_when_enforced(who)
    user_id = who["user_id"] if who else payload.get("user_id", "anonymous")

    if not question:
        return JSONResponse(status_code=400, content={"error": "question 不能为空"})

    # 先校验归属，再进图。别人的会话不能被读进提示词，也不能被续写。
    # 不带 session_id 时留给 memory_write 新建，这里不复用旧会话。
    if session_id is not None:
        from memory.short_term import ShortTermMemory

        if not ShortTermMemory().owns(user_id, session_id):
            raise HTTPException(
                status_code=404,
                detail={"code": "session_not_found", "message": "会话不存在"},
            )

    # 校验通过后才拉起图谱链路：鉴权失败 / 参数非法的请求不该付出导入与建连成本
    from graph.workflow import get_agent

    t0 = time.time()
    agent = get_agent()
    # 前端未传时沿用旧行为（允许抽取）。明确拒绝则只做通用回答，不读不写画像。
    raw_consent = payload.get("consent", None)
    consent_profile = True if raw_consent is None else bool(raw_consent)
    state = {
        "question": question,
        "user_id": user_id,
        "session_id": session_id,
        "consent_profile": consent_profile,
    }

    from observability.tracing import record_result, redact_trace, trace

    with trace(question, user_id) as tr:
        result = agent.invoke(state)
        elapsed_ms = (time.time() - t0) * 1000
        # 原句在进图之前就写进了 trace 的 input，图内的打码管不到它。
        # 命中时三处（message / 画像 / trace）必须用同一个占位常量。
        if tr is not None:
            record_result(
                tr,
                answer=result.get("answer", ""),
                evidence_counts=result.get("_evidence_counts", {}),
                graph_hit=result.get("_graph_hit", False),
                degraded=result.get("_degraded", False),
                elapsed_ms=elapsed_ms,
            )
        # 先记回答再打码。倒过来的话 record_result 会把刚写的占位盖回原文。
        # kind=guard 时 output 留下就医回答；缺省则 input 和 output 都是占位。
        if tr is not None and result.get("_redact"):
            from kg.sensitive import REDACT

            redact_trace(
                tr,
                REDACT,
                keep_output=result.get("_redacted_kind") == "guard",
            )

    return {
        "answer": result.get("answer", ""),
        "confidence": result.get("confidence", 0.0),
        "basis": result.get("basis", []),
        "gaps": result.get("gaps", []),
        "degraded": result.get("_degraded", False),
        "graph_hit": result.get("_graph_hit", False),
        "evidence_counts": result.get("_evidence_counts", {}),
        # 表格与子图吃的是**结构化字段**，不是让前端去正则抠回答文本：
        # 模型换个说法，正则就废了；字段不会。
        "facts": _facts(result),
        "subgraph": _subgraph(result),
        # 未收录时把登记结果结构化带出去：前端要显示编号和进度，
        # 不能让人从「已记录（编号 #170）」这句话里正则抠数字。
        "food_requests": result.get("_food_requests", []),
        "session_id": result.get("session_id") or session_id,
        "disclaimer": config.DISCLAIMER,
    }
