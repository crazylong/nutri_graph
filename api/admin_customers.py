"""企业客户列表。只查询。整组要管理员令牌。"""
from __future__ import annotations

from datetime import datetime

from fastapi import APIRouter, Depends, HTTPException, Query

import db
from api.auth import _require_valid_phone
from api.deps import require_admin
from security.crypto import phone_hash

router = APIRouter(
    prefix="/admin/customers",
    tags=["admin-customers"],
    dependencies=[Depends(require_admin)],
)

_PAGE = 20


def _day(value: str, end: bool) -> datetime | None:
    text = (value or "").strip()
    if not text:
        return None
    try:
        day = datetime.strptime(text, "%Y-%m-%d")
    except ValueError as exc:
        raise HTTPException(
            status_code=422,
            detail={"code": "bad_date", "message": "日期格式应为 YYYY-MM-DD"},
        ) from exc
    if end:
        return day.replace(hour=23, minute=59, second=59, microsecond=999999)
    return day


@router.get("")
def list_customers(
    phone: str = "",
    created_from: str = "",
    created_to: str = "",
    page: int = Query(1, ge=1),
):
    where = ["TRUE"]
    args: list = []
    if phone.strip():
        valid = _require_valid_phone(phone)
        where.append("phone_hash = %s")
        args.append(phone_hash(valid))
    start = _day(created_from, end=False)
    end = _day(created_to, end=True)
    if start is not None:
        where.append("created_at >= %s")
        args.append(start)
    if end is not None:
        where.append("created_at <= %s")
        args.append(end)
    clause = " AND ".join(where)
    offset = (page - 1) * _PAGE
    with db.connect() as conn:
        cur = conn.cursor()
        cur.execute(f"SELECT count(*) FROM customer WHERE {clause}", args)
        total = int(cur.fetchone()[0])
        cur.execute(
            "SELECT phone_mask, created_at FROM customer "
            f"WHERE {clause} ORDER BY created_at DESC LIMIT %s OFFSET %s",
            [*args, _PAGE, offset],
        )
        items = []
        for mask, created in cur.fetchall():
            items.append({
                "phone_mask": mask,
                "created_at": created.isoformat() if hasattr(created, "isoformat") else created,
            })
    return {"items": items, "total": total, "page": page, "page_size": _PAGE}
