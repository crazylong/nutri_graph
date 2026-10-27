"""
api/chat.py —— 主问答入口

身份优先级：已登录用户 > 匿名身份 > payload.user_id
（最后一项只在 AUTH_REQUIRED=false 的开发模式生效；
  AUTH_REQUIRED=true 时伪造 user_id 与匿名身份都会被拒 —— 这正是 needs 13 堵的口子）
"""
from __future__ import annotations

import time

from fastapi import APIRouter, Depends
from fastapi.responses import JSONResponse

import config
from api.deps import get_principal, require_user_when_enforced

router = APIRouter(tags=["chat"])


@router.post("/chat")
def chat(payload: dict, who: dict | None = Depends(get_principal)):
    """走 LangGraph 检索链路：guard → link → route → graph/vector → assemble → generate。"""
    question = payload.get("question", "")
    session_id = payload.get("session_id")

    who = require_user_when_enforced(who)
    user_id = who["user_id"] if who else payload.get("user_id", "anonymous")

    if not question:
        return JSONResponse(status_code=400, content={"error": "question 不能为空"})

    # 校验通过后才拉起图谱链路：鉴权失败 / 参数非法的请求不该付出导入与建连成本
    from graph.workflow import get_agent

    t0 = time.time()
    agent = get_agent()
    state = {"question": question, "user_id": user_id, "session_id": session_id}

    from observability.tracing import record_result, trace

    with trace(question, user_id) as tr:
        result = agent.invoke(state)
        elapsed_ms = (time.time() - t0) * 1000
        if tr is not None:
            record_result(
                tr,
                answer=result.get("answer", ""),
                evidence_counts=result.get("_evidence_counts", {}),
                graph_hit=result.get("_graph_hit", False),
                degraded=result.get("_degraded", False),
                elapsed_ms=elapsed_ms,
            )

    return {
        "answer": result.get("answer", ""),
        "confidence": result.get("confidence", 0.0),
        "basis": result.get("basis", []),
        "gaps": result.get("gaps", []),
        "degraded": result.get("_degraded", False),
        "graph_hit": result.get("_graph_hit", False),
        "evidence_counts": result.get("_evidence_counts", {}),
        # 未收录时把登记结果结构化带出去：前端要显示编号和进度，
        # 不能让人从「已记录（编号 #170）」这句话里正则抠数字。
        "food_requests": result.get("_food_requests", []),
        "session_id": result.get("session_id") or session_id,
        "disclaimer": config.DISCLAIMER,
    }
