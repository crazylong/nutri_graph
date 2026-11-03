"""后台用户列表。整组要管理员令牌。"""
from __future__ import annotations

import psycopg
from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel

import db
from api.auth import _require_valid_phone
from api.deps import require_admin
from kb.common import DEFAULT_TENANT
from security.crypto import encrypt_phone, phone_hash, phone_mask

router = APIRouter(prefix="/admin/users", tags=["admin-users"], dependencies=[Depends(require_admin)])

_PAGE = 20
_ROLES = {"user", "admin"}


class UserCreate(BaseModel):
    phone: str
    username: str = ""
    role: str = "user"


class UserPatch(BaseModel):
    username: str | None = None
    role: str | None = None


def _iso(value):
    if value is not None and hasattr(value, "isoformat"):
        return value.isoformat()
    return value


def _row(row: tuple) -> dict:
    user_id, username, mask, role, created_at, last_login_at = row
    return {
        "user_id": str(user_id),
        "username": username,
        "phone_mask": mask,
        "role": role,
        "created_at": _iso(created_at),
        "last_login_at": _iso(last_login_at),
    }


@router.get("")
def list_users(
    phone: str = "",
    username: str = "",
    role: str = "",
    page: int = Query(1, ge=1),
):
    where = ["TRUE"]
    args: list = []
    if phone.strip():
        valid = _require_valid_phone(phone)
        where.append("phone_hash = %s")
        args.append(phone_hash(valid))
    if username.strip():
        escaped = username.strip().replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
        where.append("username ILIKE %s ESCAPE '\\'")
        args.append(f"%{escaped}%")
    if role.strip():
        if role not in _ROLES:
            raise HTTPException(status_code=422, detail={"code": "bad_role", "message": "角色只能是管理员或普通用户"})
        where.append("role = %s")
        args.append(role)
    clause = " AND ".join(where)
    offset = (max(page, 1) - 1) * _PAGE
    with db.connect() as conn:
        cur = conn.cursor()
        cur.execute(f"SELECT count(*) FROM app_user WHERE {clause}", args)
        total = int(cur.fetchone()[0])
        cur.execute(
            "SELECT user_id, username, phone_mask, role, created_at, last_login_at "
            f"FROM app_user WHERE {clause} "
            "ORDER BY created_at DESC LIMIT %s OFFSET %s",
            [*args, _PAGE, offset],
        )
        items = [_row(row) for row in cur.fetchall()]
    return {"items": items, "total": total, "page": page, "page_size": _PAGE}


@router.post("")
def create_user(req: UserCreate):
    if req.role not in _ROLES:
        raise HTTPException(status_code=422, detail={"code": "bad_role", "message": "角色只能是管理员或普通用户"})
    phone = _require_valid_phone(req.phone)
    try:
        with db.connect() as conn:
            cur = conn.cursor()
            cur.execute(
                "INSERT INTO app_user (phone_enc, phone_hash, phone_mask, role, username) "
                "VALUES (%s, %s, %s, %s, %s) RETURNING user_id, username, phone_mask, role, created_at, last_login_at",
                (encrypt_phone(phone), phone_hash(phone), phone_mask(phone), req.role, req.username.strip()),
            )
            created = cur.fetchone()
            cur.execute(
                "INSERT INTO tenant_user (tenant_name, user_id) VALUES (%s, %s) ON CONFLICT DO NOTHING",
                (DEFAULT_TENANT, created[0]),
            )
    except psycopg.errors.UniqueViolation:
        raise HTTPException(
            status_code=409,
            detail={"code": "phone_exists", "message": "这个手机号已经有账号"},
        ) from None
    return _row(created)


@router.put("/{user_id}")
def update_user(user_id: str, req: UserPatch):
    if req.role is not None and req.role not in _ROLES:
        raise HTTPException(status_code=422, detail={"code": "bad_role", "message": "角色只能是管理员或普通用户"})
    with db.connect() as conn:
        cur = conn.cursor()
        cur.execute("SELECT role, username FROM app_user WHERE user_id = %s", (user_id,))
        row = cur.fetchone()
        if row is None:
            raise HTTPException(status_code=404, detail={"code": "not_found", "message": "用户不存在"})
        if row[0] == "admin" and req.role == "user":
            raise HTTPException(
                status_code=403,
                detail={"code": "forbidden", "message": "本期不能把管理员改为普通用户"},
            )
        role_sql = req.role if req.role is not None else row[0]
        username = row[1] if req.username is None else req.username.strip()
        cur.execute(
            "UPDATE app_user SET username = %s, role = %s WHERE user_id = %s "
            "RETURNING user_id, username, phone_mask, role, created_at, last_login_at",
            (username, role_sql, user_id),
        )
        updated = cur.fetchone()
    return _row(updated)


@router.delete("/{user_id}")
def delete_user(user_id: str):
    with db.connect() as conn:
        cur = conn.cursor()
        cur.execute("SELECT role FROM app_user WHERE user_id = %s", (user_id,))
        row = cur.fetchone()
        if row is None:
            raise HTTPException(status_code=404, detail={"code": "not_found", "message": "用户不存在"})
        if row[0] == "admin":
            raise HTTPException(
                status_code=409,
                detail={"code": "admin_undeletable", "message": "管理员不能删除"},
            )
        cur.execute("DELETE FROM app_user WHERE user_id = %s", (user_id,))
    return {"ok": True}
