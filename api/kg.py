"""
api/kg.py —— 知识库相关接口（Phase 9.1：未收录食材的登记与查询）

和 /admin 的分工：
  这里是「用户侧」—— 谁都能提交一个词、查自己提交的进度；
  /admin 是「运营侧」—— 看全部待审、批量通过/驳回。
"""
from __future__ import annotations

import logging

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field

from api.deps import get_principal
from kg import request as food_request

log = logging.getLogger("api.kg")

router = APIRouter(prefix="/kg", tags=["kg"])


class FoodRequestIn(BaseModel):
    food: str = Field(..., min_length=1, max_length=32, description="食物名，如「黑豆」或「黑豆粥」")


def _who(principal: dict | None) -> str:
    return principal["user_id"] if principal else "anonymous"


@router.post("/food/request")
def submit_food_request(payload: FoodRequestIn, who: dict | None = Depends(get_principal)):
    """登记一个未收录的食材。

    幂等：同一个词反复提交只累加 hit_count，不会产生多条记录。
    限流：每人每天最多 N 个**新**条目（配置见 FOOD_REQUEST_DAILY_LIMIT_PER_USER）。
    """
    result = food_request.register(payload.food, who=_who(who), source="api")
    return {
        "action": result.action,
        "term": result.term,
        "food_key": result.food_key,
        "normalized_by": result.method,
        "request_id": result.request_id,
        "status": result.status,
        "hit_count": result.hit_count,
        "suggestion": result.suggestion,
        "code": result.code,
        "message": result.message,
    }


@router.get("/food/request/{request_id}")
def food_request_status(request_id: int, who: dict | None = Depends(get_principal)):
    """查自己提交的那一条。别人的 id 一律按不存在处理，不暴露存在性。"""
    row = food_request.get_request(request_id, _who(who))
    if row is None:
        raise HTTPException(
            status_code=404,
            detail={"code": "not_found", "message": "没有这条登记记录"},
        )
    return row
