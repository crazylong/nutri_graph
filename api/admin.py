"""
api/admin.py —— 管理员接口（needs 13 授权的落地对象，也是 Phase 9 审核闭环的入口）

整个 router 挂 `require_admin`：不用每个函数都写一遍，也不会因为新加接口忘了鉴权 ——
「忘挂权限」是这类后台最常见的越权来源。

约定：
  * 表不存在时返回 503 并给出该跑哪条命令，而不是 500 堆栈
    （业务表由 migrations/002_business.sql 建；老库可能还没跑迁移）
  * 审核动作只改状态，不直接写主图 —— 主图仍由 kg.merge 统一 MERGE
"""
from __future__ import annotations

import logging

import psycopg
from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel, Field

import config
from api.deps import require_admin

log = logging.getLogger("api.admin")

router = APIRouter(prefix="/admin", tags=["admin"], dependencies=[Depends(require_admin)])

_MISSING_TABLE_HINT = "请先运行 python -m scripts.migrate 建业务表"


def _table_exists(conn, name: str) -> bool:
    cur = conn.cursor()
    cur.execute("SELECT to_regclass(%s) IS NOT NULL", (f"public.{name}",))
    return bool(cur.fetchone()[0])


class RejectRequest(BaseModel):
    reason: str = Field("", max_length=500)


class ApproveBundleRequest(BaseModel):
    item_ids: list[int] | None = Field(None, description="只采用这些项；空表示全部")
    merge: bool = Field(True, description="通过后立即合并进主图")


# ============================================================
# 运营概览
# ============================================================
@router.get("/stats")
def stats():
    """只依赖 migrations 建的表，任何时候都能用。"""
    with psycopg.connect(config.PG_DSN) as conn:
        cur = conn.cursor()
        cur.execute("SELECT status, count(*) FROM app_user GROUP BY status")
        users = {row[0]: row[1] for row in cur.fetchall()}

        cur.execute(
            "SELECT count(*) FROM sms_send_log "
            "WHERE ok AND created_at > now() - interval '1 day'"
        )
        sms_today = cur.fetchone()[0]

        cur.execute(
            "SELECT count(*) FROM sms_verify_attempt WHERE ok = false "
            "AND created_at > now() - interval '1 day'"
        )
        verify_fail_today = cur.fetchone()[0]

        pending = None
        if _table_exists(conn, "kg_candidate"):
            cur.execute("SELECT count(*) FROM kg_candidate WHERE status = 'pending'")
            pending = cur.fetchone()[0]

        # 未收录食材的闭环指标。这里刻意叫「解决率」而不是「覆盖率」：
        # 分母是「用户问过但库里没有的食材」，分子是「已经补进主图的」。
        # 真正的知识库覆盖率需要「总提问数」做分母，那要另加请求计数，现在没有。
        pending_bundles = None
        food_requests = None
        if _table_exists(conn, "kg_ingredient_request"):
            cur.execute("SELECT status, count(*) FROM kg_ingredient_request GROUP BY status")
            food_requests = {row[0]: row[1] for row in cur.fetchall()}
            total = sum(food_requests.values())
            approved = food_requests.get("approved", 0)
            food_requests["total"] = total
            food_requests["resolution_rate"] = round(approved / total, 3) if total else None
        if _table_exists(conn, "kg_food_bundle"):
            cur.execute("SELECT count(*) FROM kg_food_bundle WHERE status = 'pending_review'")
            pending_bundles = cur.fetchone()[0]

    active = users.get("active", 0)
    return {
        "users": {
            "active": active,
            "deleted": users.get("deleted", 0),
            "locked": users.get("locked", 0),
            "total": sum(users.values()),
        },
        "sms": {
            "sent_today": sms_today,
            "daily_quota": config.SMS_MAX_GLOBAL_DAY,
            "verify_failed_today": verify_fail_today,
            "provider": config.SMS_PROVIDER,
        },
        "kg": {
            "pending_candidates": pending,
            "pending_bundles": pending_bundles,
            "food_requests": food_requests,
        },
    }


# ============================================================
# 候选关系审核（kg_candidate）
# ============================================================
@router.get("/kg/candidates")
def list_candidates(
    status: str = Query("pending", pattern="^(pending|approved|rejected|merged)$"),
    limit: int = Query(50, ge=1, le=500),
):
    with psycopg.connect(config.PG_DSN) as conn:
        if not _table_exists(conn, "kg_candidate"):
            raise HTTPException(
                status_code=503,
                detail={"code": "table_missing", "message": f"kg_candidate 表不存在，{_MISSING_TABLE_HINT}"},
            )
        cur = conn.cursor()
        cur.execute(
            "SELECT id, batch_id, src_id, rel, dst_id, dst_name, claim_text, "
            "evidence_type, evidence_level, confidence, source_locator "
            "FROM kg_candidate WHERE status = %s ORDER BY id LIMIT %s",
            (status, limit),
        )
        cols = [d[0] for d in cur.description]
        rows = [dict(zip(cols, r)) for r in cur.fetchall()]
    return {"status": status, "count": len(rows), "items": rows}


def _set_status(candidate_id: int, status: str, reviewer: str, reason: str = "") -> dict:
    with psycopg.connect(config.PG_DSN) as conn:
        if not _table_exists(conn, "kg_candidate"):
            raise HTTPException(
                status_code=503,
                detail={"code": "table_missing", "message": f"kg_candidate 表不存在，{_MISSING_TABLE_HINT}"},
            )
        cur = conn.cursor()
        cur.execute(
            "UPDATE kg_candidate SET status = %s, reviewer = %s, reviewed_at = now() "
            "WHERE id = %s AND status IN ('pending', 'approved') RETURNING id, status",
            (status, reviewer, candidate_id),
        )
        row = cur.fetchone()
        if row is None:
            raise HTTPException(
                status_code=404,
                detail={"code": "not_found", "message": "候选不存在或已处理"},
            )
    if reason:
        log.info("候选 %s 被 %s 驳回，原因：%s", candidate_id, reviewer, reason)
    return {"id": row[0], "status": row[1]}


@router.post("/kg/candidates/{candidate_id}/approve")
def approve_candidate(candidate_id: int, admin: dict = Depends(require_admin)):
    return _set_status(candidate_id, "approved", admin["user_id"])


@router.post("/kg/candidates/{candidate_id}/reject")
def reject_candidate(
    candidate_id: int, req: RejectRequest, admin: dict = Depends(require_admin)
):
    return _set_status(candidate_id, "rejected", admin["user_id"], req.reason)


# ============================================================
# 未收录食材补录审核（kg_food_bundle → 候选 → 主图）
# ============================================================
_BUNDLE_MISSING = "kg_food_bundle 表不存在，请先运行 python -m scripts.migrate"


def _require_bundle_table(conn) -> None:
    if not _table_exists(conn, "kg_food_bundle"):
        raise HTTPException(
            status_code=503,
            detail={"code": "table_missing", "message": _BUNDLE_MISSING},
        )


@router.get("/kg/bundles")
def list_bundles(
    status: str = Query(
        "pending_review",
        pattern="^(draft|verified|pending_review|approved|merged|rejected)$",
    ),
    limit: int = Query(50, ge=1, le=200),
):
    with psycopg.connect(config.PG_DSN) as conn:
        _require_bundle_table(conn)
    from kg import promote

    rows = promote.list_bundles(status, limit)
    return {"status": status, "count": len(rows), "items": rows}


@router.get("/kg/bundles/{bundle_id}")
def get_bundle(bundle_id: int):
    from kg import promote

    try:
        return promote.get_bundle(bundle_id)
    except promote.PromoteError as exc:
        raise HTTPException(
            status_code=404, detail={"code": "not_found", "message": str(exc)}
        ) from exc


@router.post("/kg/bundles/{bundle_id}/approve")
def approve_bundle(
    bundle_id: int, req: ApproveBundleRequest, admin: dict = Depends(require_admin)
):
    """通过补录产物：翻译成节点/边候选，默认立即合并进主图。

    注意这里**不直接写 Neo4j** —— 主图写入口只有 kg.merge 一个。
    """
    from kg import promote

    try:
        return promote.approve_bundle(
            bundle_id,
            reviewer=admin["user_id"],
            item_ids=req.item_ids,
            merge_now=req.merge,
        )
    except promote.PromoteError as exc:
        msg = str(exc)
        raise HTTPException(
            status_code=404 if "不存在" in msg else 409,
            detail={
                "code": "not_found" if "不存在" in msg else "promote_rejected",
                "message": msg,
            },
        ) from exc


@router.post("/kg/bundles/{bundle_id}/reject")
def reject_bundle(bundle_id: int, req: RejectRequest, admin: dict = Depends(require_admin)):
    from kg import promote

    try:
        return promote.reject_bundle(bundle_id, reviewer=admin["user_id"], reason=req.reason)
    except promote.PromoteError as exc:
        msg = str(exc)
        raise HTTPException(
            status_code=404 if "不存在" in msg else 409,
            detail={
                "code": "not_found" if "不存在" in msg else "promote_rejected",
                "message": msg,
            },
        ) from exc
