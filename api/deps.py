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
from security.tokens import InvalidTokenError, decode_access, decode_anon

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


def _resolve(token: str | None) -> dict | None:
    if not token:
        return None
    try:
        return _from_access(token)
    except InvalidTokenError as user_err:
        # 不是 access token 就再试匿名身份；两种都不成立时，
        # 报第一个错误（例如「已过期」比「类型不正确」对用户更有意义）
        try:
            return _from_anon(token)
        except InvalidTokenError:
            raise HTTPException(
                status_code=401,
                detail={"code": "invalid_token", "message": str(user_err)},
                headers=_UNAUTHORIZED,
            ) from user_err


def get_principal(token: str | None = Depends(oauth2_scheme)) -> dict | None:
    """有令牌返回身份（可能是匿名），没有返回 None。"""
    return _resolve(token)


def get_current_user(token: str | None = Depends(oauth2_scheme)) -> dict:
    """必须是已登录用户；匿名身份一律拒绝（匿名不是账号，没有 phone/role）。"""
    principal = _resolve(token)
    if principal is None or principal["kind"] != "user":
        raise HTTPException(
            status_code=401,
            detail={"code": "unauthorized", "message": "请先登录"},
            headers=_UNAUTHORIZED,
        )
    return principal


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
