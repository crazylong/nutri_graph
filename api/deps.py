"""
api/deps.py —— 鉴权依赖

三种身份，一条令牌通道：
  kind="user"  已登录用户（typ=access）
  kind="anon"  匿名身份（typ=anon），可以先体验，登录后归并
  None         没带令牌

依赖分级：
  get_current_user   必须**已登录**（匿名不算，401）
  get_principal      有令牌就给身份，没有就 None（/chat 用这条）
  require_admin      必须是 admin（403）

为什么用 OAuth2PasswordBearer：只为从 Authorization: Bearer 里取令牌，不自建 header 解析。
真正的校验在 security/tokens.py。
"""
from __future__ import annotations

import logging

import psycopg
from fastapi import Depends, HTTPException, Request
from fastapi.security import OAuth2PasswordBearer

import config
from security.tokens import InvalidTokenError, decode_access, decode_anon, decode_claims

import db

log = logging.getLogger("api.deps")

oauth2_scheme = OAuth2PasswordBearer(tokenUrl="/auth/token", auto_error=False)

_UNAUTHORIZED = {"WWW-Authenticate": "Bearer"}


def client_ip(request: Request) -> str:
    """取真实客户端 IP。

    nginx 把 `$remote_addr` 写进 X-Real-IP，会覆盖客户端自带的同名头。
    X-Forwarded-For 第一段是客户端自己塞的（`proxy_add_x_forwarded_for`
    把它留在最左边），不能拿来做频控。
    没有 X-Real-IP 时用连接对端。生产由 nginx 反代，直连只发生在本机。
    """
    real = request.headers.get("x-real-ip", "").strip()
    if real:
        return real
    return request.client.host if request.client else ""


def load_user(user_id: str) -> dict | None:
    with db.connect() as conn:
        cur = conn.cursor()
        cur.execute(
            "SELECT user_id, phone_mask, role, status FROM app_user WHERE user_id = %s",
            (user_id,),
        )
        row = cur.fetchone()
    if row is None:
        return None
    return {
        "user_id": str(row[0]),
        "phone_mask": row[1],
        "role": row[2],
        "status": row[3],
    }


def _from_access(token: str) -> dict:
    claims = decode_access(token)
    user = load_user(claims["sub"])
    if user is None or user["status"] != "active":
        raise InvalidTokenError("账号不存在或不可用")
    return {"kind": "user", **user}


def _from_anon(token: str) -> dict:
    claims = decode_anon(token)
    return {
        "kind": "anon",
        "user_id": claims["sub"],
        "role": "anon",
        "phone_mask": "",
        "status": "active",
    }


def load_customer(user_id: str) -> dict | None:
    with db.connect() as conn:
        cur = conn.cursor()
        cur.execute(
            "SELECT user_id, phone_mask, status FROM customer WHERE user_id = %s",
            (user_id,),
        )
        row = cur.fetchone()
    if row is None:
        return None
    return {
        "user_id": str(row[0]),
        "phone_mask": row[1],
        "role": "user",
        "status": row[2],
    }


def _unauthorized(message: str) -> HTTPException:
    return HTTPException(
        status_code=401,
        detail={"code": "invalid_token", "message": message},
        headers=_UNAUTHORIZED,
    )


def get_principal(token: str | None = Depends(oauth2_scheme)) -> dict | None:
    """食疗前台身份：客户令牌或匿名令牌。员工令牌不能当客户用。"""
    if not token:
        return None
    try:
        claims = decode_claims(token)
    except InvalidTokenError as exc:
        raise _unauthorized(str(exc)) from exc
    typ = claims.get("typ")
    if typ == "access":
        raise HTTPException(
            status_code=403,
            detail={"code": "staff_token", "message": "请使用食疗前台账号登录"},
        )
    if typ == "customer":
        user = load_customer(claims["sub"])
        if user is None or user["status"] != "active":
            raise _unauthorized("账号不存在或不可用")
        return {"kind": "user", **user}
    if typ == "anon":
        try:
            return _from_anon(token)
        except InvalidTokenError as exc:
            raise _unauthorized(str(exc)) from exc
    raise _unauthorized("令牌类型不正确")


def get_current_user(token: str | None = Depends(oauth2_scheme)) -> dict:
    """必须是员工令牌。客户令牌和匿名身份一律拒绝。"""
    if not token:
        raise HTTPException(
            status_code=401,
            detail={"code": "unauthorized", "message": "请先登录"},
            headers=_UNAUTHORIZED,
        )
    try:
        return _from_access(token)
    except InvalidTokenError as exc:
        raise HTTPException(
            status_code=401,
            detail={"code": "invalid_token", "message": str(exc)},
            headers=_UNAUTHORIZED,
        ) from exc


def require_admin(user: dict = Depends(get_current_user)) -> dict:
    if user.get("role") != "admin":
        raise HTTPException(
            status_code=403,
            detail={"code": "forbidden", "message": "需要管理员权限"},
        )
    return user


def require_user_when_enforced(principal: dict | None) -> dict | None:
    """AUTH_REQUIRED=true 时，业务接口只认真实登录用户（匿名也不行）。"""
    if not config.AUTH_REQUIRED:
        return principal
    if principal is None or principal["kind"] != "user":
        raise HTTPException(
            status_code=401,
            detail={"code": "unauthorized", "message": "请先登录"},
            headers=_UNAUTHORIZED,
        )
    return principal
