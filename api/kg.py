"""
api/kg.py —— 知识库相关接口

三块：
  1. 未收录食材的登记与查询（Phase 9.1）
  2. 子图可视化：单食材邻域 / 两个食材之间的关系（面试演示「图谱长什么样」用）
  3. 与 /admin 的分工：
       这里是「用户侧」—— 谁都能提交一个词、查自己提交的进度、看图谱；
       /admin 是「运营侧」—— 看全部待审、批量通过/驳回。
"""
from __future__ import annotations

import logging

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel, Field

import config
from api.deps import get_principal
from kg import request as food_request
from kg import subgraph as sg
from kg.subgraph import DEFAULT_RELS

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


# ============================================================
# 子图可视化
# ============================================================
# 说明：这几个接口读的是**已审核进主图**的数据，不含候选表里的待审内容；
# 也不做用户画像过滤（过敏/慢病是问答链路的约束，探索视图要看全貌）。
def _not_found(term: str, near: list[dict] | None = None) -> HTTPException:
    msg = f"图谱里没有「{term}」"
    if near:
        msg += "，你是不是想查：" + "、".join(
            n.get("candidate_alias") or n.get("candidate_id") or "" for n in near
        )
    return HTTPException(status_code=404, detail={"code": "not_found", "message": msg})


@router.get("/subgraph")
def ingredient_subgraph(
    food: str = Query("", description="食材名，如「黑豆」"),
    cid: str = Query("", description="已知 canonical_id 时直接给这个，跳过实体链接"),
    depth: int = Query(1, ge=1, le=2, description="1=只看一跳邻域；2=再扩一层"),
    rels: str = Query("", description="逗号分隔的关系类型，留空=全部"),
    who: dict | None = Depends(get_principal),
):
    """一个食材的邻域子图：它含什么营养素、和谁搭配、谁该避开它。

    返回的 nodes/edges 可以直接画 —— 前端不需要再做任何图算法。
    """
    if not cid:
        hit = sg.resolve(food or cid)
        if not hit or not hit.get("canonical_id"):
            raise _not_found(food, (hit or {}).get("near_misses"))
        cid = hit["canonical_id"]
    else:
        hit = None

    node = sg.fetch_node(cid)
    if not node:
        raise _not_found(cid or food)

    rel_list = [r.strip() for r in rels.split(",") if r.strip()] or DEFAULT_RELS
    unknown = [r for r in rel_list if r not in DEFAULT_RELS]
    if unknown:
        raise HTTPException(
            status_code=400,
            detail={"code": "bad_rels", "message": f"未知关系类型：{','.join(unknown)}"},
        )

    try:
        g = sg.neighborhood(cid, depth=depth, rels=rel_list)
    except Exception as exc:  # noqa: BLE001 —— 图不可用时给一句人话，不是 500
        log.warning("子图查询失败 cid=%s: %s: %s", cid, type(exc).__name__, exc)
        raise HTTPException(
            status_code=503,
            detail={"code": "graph_unavailable", "message": "图谱暂时连不上，稍后重试。"},
        ) from exc

    return {
        "anchor": {"canonical_id": cid, "label": node["label"], "name": node["name"]},
        "resolved": hit,
        "subgraph": g,
        "disclaimer": config.DISCLAIMER,
    }


@router.get("/relation")
def ingredient_relation(
    a: str = Query("", description="食材 A，如「黑豆」"),
    b: str = Query("", description="食材 B，如「菠菜」"),
    hops: int = Query(3, ge=1, le=4, description="两食材之间最大跳数"),
    expand: bool = Query(False, description="是否把两侧的邻域也画出来"),
    who: dict | None = Depends(get_principal),
):
    """两个食材在图里是什么关系。

    kind:
      direct —— 有直接边（搭配 / 不宜同食 / 替代）
      path   —— 没有直接边，但存在 N 跳路径（例如都富含同一种营养素）
      none   —— 图上暂时连不上
    """
    if not a or not b:
        raise HTTPException(
            status_code=400,
            detail={"code": "bad_params", "message": "需要两个食材名（a 和 b）"},
        )

    ra, rb = sg.resolve(a), sg.resolve(b)
    if not ra or not ra.get("canonical_id"):
        raise _not_found(a, (ra or {}).get("near_misses"))
    if not rb or not rb.get("canonical_id"):
        raise _not_found(b, (rb or {}).get("near_misses"))

    try:
        g = sg.relation_between(
            ra["canonical_id"], rb["canonical_id"], hops=hops, expand=expand
        )
    except Exception as exc:  # noqa: BLE001
        log.warning("关系查询失败 %s~%s: %s: %s", a, b, type(exc).__name__, exc)
        raise HTTPException(
            status_code=503,
            detail={"code": "graph_unavailable", "message": "图谱暂时连不上，稍后重试。"},
        ) from exc

    return {
        "a": ra,
        "b": rb,
        "subgraph": g,
        "disclaimer": config.DISCLAIMER,
    }


class CompareIn(BaseModel):
    foods: list[str] = Field(..., min_length=2, max_length=4, description="2~4 个食材名")


@router.post("/compare")
def compare_foods(payload: CompareIn, who: dict | None = Depends(get_principal)):
    """多个食材（2~4 个）的两两关系合并成一张子图。

    与 /relation 的差别：一次性看多个食材之间「谁和谁有关系」，而不是只看一对。
    未收录的食材不挡整次请求，而是统一登记（幂等）后放进 missing 列表返回，
    让前端一次性提示「XX、XX 未收录，已反馈给管理员」。
    """
    foods = [f.strip() for f in payload.foods if f and f.strip()]
    if len(foods) < 2:
        raise HTTPException(
            status_code=400,
            detail={"code": "bad_params", "message": "至少需要两个食材"},
        )

    resolved: list[dict] = []
    missing: list[dict] = []
    for food in foods:
        r = sg.resolve(food)
        if r and r.get("canonical_id"):
            resolved.append(r)
        else:
            # 未收录：登记（幂等），记录供前端一次性汇总提示
            reg = food_request.register(food, who=_who(who), source="compare")
            missing.append({"term": food, "registered": reg.registered, "code": reg.code})

    cids = [r["canonical_id"] for r in resolved]
    g = None
    if len(cids) >= 2:
        try:
            g = sg.compare_many(cids)
        except Exception as exc:  # noqa: BLE001
            log.warning("多食材对照失败 %s: %s: %s", cids, type(exc).__name__, exc)
            raise HTTPException(
                status_code=503,
                detail={"code": "graph_unavailable", "message": "图谱暂时连不上，稍后重试。"},
            ) from exc

    return {
        "resolved": resolved,
        "missing": missing,
        "subgraph": g or {"nodes": [], "edges": [], "meta": {"kind": "compare"}},
        "disclaimer": config.DISCLAIMER,
    }
