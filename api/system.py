"""
api/system.py —— 运行时状态（模型池）

为什么独立成一个模块而不是塞进 health：
/health 是给探活用的（必须轻、必须快、不依赖业务配置）；
/llm/pool 是给人看的运维视图，会暴露额度与内部模型名，所以 AUTH_REQUIRED 下只给 admin。
"""
from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException

import config
from api.deps import get_principal

router = APIRouter(tags=["system"])


@router.get("/llm/pool")
def llm_pool(who: dict | None = Depends(get_principal)):
    """模型池现状：谁在用、下一个是谁、谁被移出池/冷却中、兜底模型是谁。"""
    if config.AUTH_REQUIRED and (who is None or who.get("role") != "admin"):
        raise HTTPException(
            status_code=403,
            detail={"code": "forbidden", "message": "需要管理员权限"},
        )
    from llm import pool_snapshot

    snap = pool_snapshot()
    return {
        "current": snap["current"],
        "queue": snap["queue"],
        "fallback": snap["fallback"],
        "models": snap["models"],
    }
