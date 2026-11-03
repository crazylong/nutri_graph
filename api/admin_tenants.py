"""租户列表。整组要管理员令牌。租户名即文档上的 tenant_id，不能改。"""
from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel

import db
from api.auth import _require_valid_phone
from api.deps import require_admin
from kb.common import DEFAULT_TENANT, KbError, check_tenant
from security.crypto import phone_hash

router = APIRouter(prefix="/admin/tenants", tags=["admin-tenants"], dependencies=[Depends(require_admin)])


class TenantCreate(BaseModel):
    name: str


class MemberAdd(BaseModel):
    phone: str


def _iso(value):
    if value is not None and hasattr(value, "isoformat"):
        return value.isoformat()
    return value


@router.get("")
def list_tenants():
    with db.connect() as conn:
        cur = conn.cursor()
        cur.execute(
            "SELECT t.name, t.created_at, count(tu.user_id) "
            "FROM tenant t LEFT JOIN tenant_user tu ON tu.tenant_name = t.name "
            "GROUP BY t.name, t.created_at ORDER BY t.name"
        )
        items = [
            {"name": name, "created_at": _iso(created), "user_count": int(count)}
            for name, created, count in cur.fetchall()
        ]
    return {"items": items}


@router.post("")
def create_tenant(req: TenantCreate):
    try:
        name = check_tenant(req.name)
    except KbError as exc:
        raise HTTPException(status_code=exc.status, detail={"code": exc.code, "message": exc.message}) from exc
    with db.connect() as conn:
        cur = conn.cursor()
        cur.execute("SELECT 1 FROM tenant WHERE name = %s", (name,))
        if cur.fetchone() is not None:
            raise HTTPException(status_code=409, detail={"code": "tenant_exists", "message": "租户已存在"})
        cur.execute(
            "INSERT INTO tenant (name) VALUES (%s) RETURNING created_at",
            (name,),
        )
        created = cur.fetchone()[0]
    return {"name": name, "created_at": _iso(created), "user_count": 0}


@router.delete("/{name}")
def delete_tenant(name: str):
    if name == DEFAULT_TENANT:
        raise HTTPException(status_code=409, detail={"code": "tenant_protected", "message": "默认租户不能删除"})
    with db.connect() as conn:
        cur = conn.cursor()
        cur.execute("SELECT 1 FROM tenant WHERE name = %s", (name,))
        if cur.fetchone() is None:
            raise HTTPException(status_code=404, detail={"code": "not_found", "message": "租户不存在"})
        cur.execute("SELECT 1 FROM kb_document WHERE tenant_id = %s LIMIT 1", (name,))
        if cur.fetchone() is not None:
            raise HTTPException(status_code=409, detail={"code": "tenant_has_docs", "message": "名下还有文档，不能删除"})
        cur.execute("DELETE FROM tenant WHERE name = %s", (name,))
    return {"ok": True}


@router.get("/{name}/users")
def list_members(name: str):
    with db.connect() as conn:
        cur = conn.cursor()
        cur.execute("SELECT 1 FROM tenant WHERE name = %s", (name,))
        if cur.fetchone() is None:
            raise HTTPException(status_code=404, detail={"code": "not_found", "message": "租户不存在"})
        cur.execute(
            "SELECT u.user_id, u.username, u.phone_mask, u.role "
            "FROM tenant_user tu JOIN app_user u ON u.user_id = tu.user_id "
            "WHERE tu.tenant_name = %s ORDER BY u.created_at",
            (name,),
        )
        items = [
            {"user_id": str(uid), "username": username, "phone_mask": mask, "role": role}
            for uid, username, mask, role in cur.fetchall()
        ]
    return {"items": items}


@router.post("/{name}/users")
def add_member(name: str, req: MemberAdd):
    phone = _require_valid_phone(req.phone)
    with db.connect() as conn:
        cur = conn.cursor()
        cur.execute("SELECT 1 FROM tenant WHERE name = %s", (name,))
        if cur.fetchone() is None:
            raise HTTPException(status_code=404, detail={"code": "not_found", "message": "租户不存在"})
        cur.execute("SELECT user_id FROM app_user WHERE phone_hash = %s", (phone_hash(phone),))
        row = cur.fetchone()
        if row is None:
            raise HTTPException(status_code=404, detail={"code": "not_found", "message": "用户不存在"})
        cur.execute(
            "INSERT INTO tenant_user (tenant_name, user_id) VALUES (%s, %s) ON CONFLICT DO NOTHING",
            (name, row[0]),
        )
    return {"ok": True}


@router.delete("/{name}/users/{user_id}")
def remove_member(name: str, user_id: str):
    with db.connect() as conn:
        cur = conn.cursor()
        cur.execute(
            "DELETE FROM tenant_user WHERE tenant_name = %s AND user_id = %s",
            (name, user_id),
        )
        if cur.rowcount == 0:
            raise HTTPException(status_code=404, detail={"code": "not_found", "message": "不在这个租户里"})
    return {"ok": True}
