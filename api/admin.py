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

import db

log = logging.getLogger("api.admin")

router = APIRouter(prefix="/admin", tags=["admin"], dependencies=[Depends(require_admin)])

_MISSING_TABLE_HINT = "请先运行 python -m scripts.migrate 建业务表"


def _table_exists(conn, name: str) -> bool:
    cur = conn.cursor()
    cur.execute("SELECT to_regclass(%s) IS NOT NULL", (f"public.{name}",))
    return bool(cur.fetchone()[0])


class RejectRequest(BaseModel):
    reason: str = Field("", max_length=500)


class ApproveCandidateRequest(BaseModel):
    confirm_sensitive: bool = Field(False, description="已人工确认 review 级敏感词")


class ApproveBundleRequest(BaseModel):
    item_ids: list[int] | None = Field(None, description="只采用这些项；空表示全部")
    merge: bool = Field(True, description="通过后立即合并进主图")
    # 命中 review 级敏感词时必须显式确认：前端要先把命中项展示给人看，
    # 由人勾一次"已人工确认"，而不是后端悄悄放行
    confirm_sensitive: bool = Field(False, description="已人工确认 review 级敏感词")


# ============================================================
# 运营概览
# ============================================================
@router.get("/stats")
def stats():
    """只依赖 migrations 建的表，任何时候都能用。"""
    with db.connect() as conn:
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
    with db.connect() as conn:
        if not _table_exists(conn, "kg_candidate"):
            raise HTTPException(
                status_code=503,
                detail={"code": "table_missing", "message": f"kg_candidate 表不存在，{_MISSING_TABLE_HINT}"},
            )
        cur = conn.cursor()
        cur.execute(
            "SELECT id, batch_id, src_id, rel, dst_id, dst_name, claim_text, "
            "evidence_type, evidence_level, confidence, source_locator, sources "
            "FROM kg_candidate WHERE status = %s ORDER BY id LIMIT %s",
            (status, limit),
        )
        cols = [d[0] for d in cur.description]
        rows = [dict(zip(cols, r)) for r in cur.fetchall()]
    from kg.sensitive import scan

    for row in rows:
        hits = []
        hits += scan(str(row.get("dst_name") or ""), field_name="name")
        hits += scan(str(row.get("claim_text") or ""), field_name="claim_text")
        row["sensitive_block"] = sum(1 for h in hits if h.action == "block")
        row["sensitive_review"] = sum(1 for h in hits if h.action == "review")
        row["sensitive_words"] = sorted({h.word for h in hits if h.action == "review"})
    return {"status": status, "count": len(rows), "items": rows}


def _set_status(
    candidate_id: int,
    status: str,
    reviewer: str,
    reason: str = "",
    confirm_sensitive: bool = False,
) -> dict:
    with db.connect() as conn:
        if not _table_exists(conn, "kg_candidate"):
            raise HTTPException(
                status_code=503,
                detail={"code": "table_missing", "message": f"kg_candidate 表不存在，{_MISSING_TABLE_HINT}"},
            )
        cur = conn.cursor()

        # 批准前先过敏感词闸。这个口直接 UPDATE kg_candidate，
        # 而 kg.merge 消费 status='approved' —— 它是页面上真在用的关系候选批准口，
        # 漏掉它等于闸只关了一半（步骤 2.4）。
        if status == "approved":
            from kg.sensitive import SensitiveBlocked, assert_approvable, log_hits, scan

            row0 = cur.execute(
                "SELECT dst_name, claim_text FROM kg_candidate WHERE id = %s",
                (candidate_id,),
            ).fetchone()
            hits = []
            if row0:
                hits += scan(str(row0[0] or ""), field_name="name")
                hits += scan(str(row0[1] or ""), field_name="claim_text")
            if hits:
                log_hits("review", hits, source="admin_set_status")
            try:
                assert_approvable(hits, confirm=confirm_sensitive)
            except SensitiveBlocked as exc:
                raise HTTPException(
                    status_code=409,
                    detail={"code": "sensitive_blocked", "message": str(exc)},
                ) from exc

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
def approve_candidate(
    candidate_id: int,
    req: ApproveCandidateRequest = ApproveCandidateRequest(),
    admin: dict = Depends(require_admin),
):
    return _set_status(
        candidate_id, "approved", admin["user_id"], confirm_sensitive=req.confirm_sensitive
    )


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
    with db.connect() as conn:
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
            confirm_sensitive=req.confirm_sensitive,
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


# ============================================================
# 未收录食物登记（kg_ingredient_request）与疑似别名
# ============================================================
_REQUEST_MISSING = "kg_ingredient_request 表不存在，请先运行 python -m scripts.migrate"
_ALIAS_CANDIDATE_MISSING = "dict_alias_candidate 表不存在，请先运行 python -m scripts.migrate"

_REQUEST_STATUSES = "queued|fetching|fetched|pending_review|approved|rejected"


class ManualFoodRequest(BaseModel):
    name: str = Field(..., min_length=1, max_length=80)


def _require_named_table(conn, name: str, message: str) -> None:
    if not _table_exists(conn, name):
        raise HTTPException(
            status_code=503,
            detail={"code": "table_missing", "message": message},
        )


def _like(keyword: str) -> str:
    escaped = keyword.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
    return f"%{escaped}%"


def _rows(cur) -> list[dict]:
    cols = [d[0] for d in cur.description]
    return [dict(zip(cols, row)) for row in cur.fetchall()]


@router.get("/kg/requests")
def list_food_requests(
    status: str = Query("", pattern=f"^({_REQUEST_STATUSES})?$"),
    keyword: str = Query("", max_length=80),
    limit: int = Query(20, ge=1, le=200),
    offset: int = Query(0, ge=0),
):
    """未收录食物。count 是符合筛选的总条数，不是本页条数。"""
    where = ["TRUE"]
    args: list = []
    if status:
        where.append("status = %s")
        args.append(status)
    if keyword.strip():
        where.append(
            "(food_key ILIKE %s ESCAPE '\\' OR raw_mention ILIKE %s ESCAPE '\\' "
            "OR normalized ILIKE %s ESCAPE '\\')"
        )
        pat = _like(keyword.strip())
        args.extend([pat, pat, pat])
    clause = " AND ".join(where)
    with db.connect() as conn:
        _require_named_table(conn, "kg_ingredient_request", _REQUEST_MISSING)
        cur = conn.cursor()
        cur.execute(f"SELECT count(*) FROM kg_ingredient_request WHERE {clause}", args)
        total = int(cur.fetchone()[0])
        cur.execute(
            "SELECT id, food_key, raw_mention, normalized, requested_by, source, "
            "hit_count, status, reject_reason, last_requested_at, created_at "
            f"FROM kg_ingredient_request WHERE {clause} "
            "ORDER BY last_requested_at DESC, id DESC LIMIT %s OFFSET %s",
            [*args, limit, offset],
        )
        items = _rows(cur)
    return {"count": total, "limit": limit, "offset": offset, "items": items}


@router.get("/kg/requests/{request_id}")
def get_food_request(request_id: int):
    with db.connect() as conn:
        _require_named_table(conn, "kg_ingredient_request", _REQUEST_MISSING)
        cur = conn.cursor()
        cur.execute(
            "SELECT id, food_key, raw_mention, normalized, requested_by, source, "
            "hit_count, status, reject_reason, last_requested_at, created_at "
            "FROM kg_ingredient_request WHERE id = %s",
            (request_id,),
        )
        rows = _rows(cur)
    if not rows:
        raise HTTPException(
            status_code=404,
            detail={"code": "not_found", "message": "登记不存在"},
        )
    return rows[0]


@router.post("/kg/requests/{request_id}/reject")
def reject_food_request(request_id: int, req: RejectRequest):
    """驳回未收录登记。理由可以空，空也写成空字符串。"""
    with db.connect() as conn:
        _require_named_table(conn, "kg_ingredient_request", _REQUEST_MISSING)
        cur = conn.cursor()
        cur.execute(
            "UPDATE kg_ingredient_request SET status = 'rejected', reject_reason = %s "
            "WHERE id = %s RETURNING id, status, reject_reason",
            (req.reason, request_id),
        )
        row = cur.fetchone()
    if row is None:
        raise HTTPException(
            status_code=404,
            detail={"code": "not_found", "message": "登记不存在"},
        )
    return {"id": row[0], "status": row[1], "reject_reason": row[2]}


@router.post("/kg/requests")
def create_food_request(req: ManualFoodRequest, admin: dict = Depends(require_admin)):
    """管理员手工登记。不占个人日限额，仍受全站日上限约束。不累加搜索次数。"""
    from kg.request import register

    result = register(
        req.name,
        who=admin["user_id"],
        source="admin",
        skip_user_limit=True,
        count_hit=False,
    )
    if result.code == "global_daily_limit" or result.action == "rate_limited":
        raise HTTPException(
            status_code=429,
            detail={"code": result.code or "rate_limited", "message": result.message},
        )
    if result.action == "not_ready":
        raise HTTPException(
            status_code=503,
            detail={"code": result.code or "not_ready", "message": result.message},
        )
    if not result.registered:
        raise HTTPException(
            status_code=409,
            detail={
                "code": result.code or result.action,
                "message": result.message,
            },
        )
    return {
        "id": result.request_id,
        "food_key": result.food_key,
        "status": result.status,
        "hit_count": result.hit_count,
        "action": result.action,
        "source": "admin",
        "message": result.message,
    }


@router.get("/kg/alias-candidates")
def list_alias_candidates(
    status: str = Query("", pattern="^(pending|approved|rejected)?$"),
    limit: int = Query(20, ge=1, le=200),
    offset: int = Query(0, ge=0),
):
    """疑似别名只读。status 以库里的当前值为准，补录合并可能已把它改成 approved。"""
    where = "TRUE" if not status else "status = %s"
    args: list = [status] if status else []
    with db.connect() as conn:
        _require_named_table(conn, "dict_alias_candidate", _ALIAS_CANDIDATE_MISSING)
        cur = conn.cursor()
        cur.execute(f"SELECT count(*) FROM dict_alias_candidate WHERE {where}", args)
        total = int(cur.fetchone()[0])
        cur.execute(
            "SELECT id, entity_type, alias, alias_norm, suggested_canonical_id, "
            "status, created_at "
            f"FROM dict_alias_candidate WHERE {where} "
            "ORDER BY id DESC LIMIT %s OFFSET %s",
            [*args, limit, offset],
        )
        items = _rows(cur)
    return {"count": total, "limit": limit, "offset": offset, "items": items}
