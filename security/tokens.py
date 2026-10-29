"""
security/tokens.py —— access token（JWT）+ refresh token（PG 可吊销）

令牌语义对齐 OAuth2（token_type / expires_in / access + refresh），
但不实现完整授权服务器：本项目是第一方自建登录，没有第三方代授权场景。

两条安全设计：
  1. refresh 存哈希不存明文 —— 数据库泄露也拿不到可用的 refresh token
  2. 旋转 + 重放检测 —— refresh 用一次就作废；若有人拿旧的再来一次，
     说明令牌已泄露，直接把整个 family 吊销，逼双方重新登录
"""
from __future__ import annotations

import datetime as dt
import logging
import uuid

import jwt
import psycopg

import config
from security.crypto import jwt_secret, new_token, sha256_hex

import db

log = logging.getLogger("security.tokens")


class InvalidTokenError(Exception):
    """令牌无效 / 过期 / 已被吊销。"""


class TokenReuseError(InvalidTokenError):
    """检测到 refresh token 重放：整族已吊销。"""


def _now() -> dt.datetime:
    return dt.datetime.now(dt.timezone.utc)



# ============================================================
# access token
# ============================================================
def make_access(user_id: str, role: str) -> tuple[str, int]:
    now = _now()
    exp = now + dt.timedelta(seconds=config.ACCESS_TTL_S)
    payload = {
        "sub": str(user_id),
        "role": role,
        "typ": "access",
        "iss": config.JWT_ISSUER,
        "jti": uuid.uuid4().hex,
        "iat": int(now.timestamp()),
        "exp": int(exp.timestamp()),
    }
    token = jwt.encode(payload, jwt_secret(), algorithm=config.JWT_ALG)
    return token, config.ACCESS_TTL_S


def decode_access(token: str) -> dict:
    try:
        claims = jwt.decode(
            token,
            jwt_secret(),
            algorithms=[config.JWT_ALG],
            issuer=config.JWT_ISSUER,
            options={"require": ["exp", "sub", "iss"]},
        )
    except jwt.ExpiredSignatureError as exc:
        raise InvalidTokenError("令牌已过期，请刷新或重新登录") from exc
    except jwt.InvalidTokenError as exc:
        raise InvalidTokenError("令牌无效") from exc
    if claims.get("typ") != "access":
        raise InvalidTokenError("令牌类型不正确")
    return claims


# ============================================================
# refresh token
# ============================================================
def _store_refresh(conn, user_id: str, family_id: str, expires_at: dt.datetime) -> tuple[str, str]:
    """写入一条 refresh，返回 (明文, jti)。明文只在这一刻存在，库里只有哈希。"""
    raw = new_token(32)
    cur = conn.cursor()
    cur.execute(
        "INSERT INTO refresh_token (user_id, token_hash, family_id, expires_at) "
        "VALUES (%s, %s, %s, %s) RETURNING jti",
        (user_id, sha256_hex(raw), family_id, expires_at),
    )
    return raw, str(cur.fetchone()[0])


def issue_tokens(user_id: str, role: str) -> dict:
    """登录成功：新建一个 family，签发 access + refresh。"""
    expires_at = _now() + dt.timedelta(days=config.REFRESH_TTL_D)
    family_id = str(uuid.uuid4())
    with db.connect() as conn:
        refresh, _ = _store_refresh(conn, user_id, family_id, expires_at)
    access, ttl = make_access(user_id, role)
    return {
        "access_token": access,
        "refresh_token": refresh,
        "token_type": "bearer",
        "expires_in": ttl,
    }


def rotate_refresh(raw_refresh: str) -> dict:
    """用 refresh 换新的一对令牌（旋转）。检测到重放则吊销整族。

    注意：吊销必须在退出 with 块（提交）之后再抛异常 ——
    在 with 内抛异常会触发 psycopg 回滚，吊销动作会一起被撤销，
    等于「检测到泄露却没真正封掉」。
    """
    token_hash = sha256_hex(raw_refresh)
    reuse_family: str | None = None
    issued: tuple[str, str, str] | None = None

    with db.connect() as conn:
        cur = conn.cursor()
        cur.execute(
            "SELECT jti, user_id, family_id, expires_at, revoked_at, replaced_by "
            "FROM refresh_token WHERE token_hash = %s",
            (token_hash,),
        )
        row = cur.fetchone()
        if row is None:
            raise InvalidTokenError("刷新令牌不存在")

        jti, user_id, family_id, expires_at, revoked_at, replaced_by = row

        if revoked_at is not None:
            # 两种「已吊销」必须分开，否则正常登出会被当成攻击：
            #   replaced_by 有值 = 它已经被旋转换掉了，现在又被拿出来用 → 真·重放，整族封掉
            #   replaced_by 为空 = 登出/强制下线/注销吊销的 → 只是会话结束了，提示重新登录
            # 判错的代价：用户点了退出，下次回来看到「检测到异常登录」，
            # 会以为账号被盗，而实际上什么都没发生。
            if replaced_by is None:
                raise InvalidTokenError("登录已失效，请重新登录")
            conn.execute(
                "UPDATE refresh_token SET revoked_at = now() "
                "WHERE family_id = %s AND revoked_at IS NULL",
                (family_id,),
            )
            reuse_family = str(family_id)
        elif expires_at < _now():
            raise InvalidTokenError("刷新令牌已过期，请重新登录")

        else:
            cur.execute("SELECT role, status FROM app_user WHERE user_id = %s", (user_id,))
            urow = cur.fetchone()
            if urow is None or urow[1] != "active":
                raise InvalidTokenError("账号不可用")
            role = urow[0]

            new_expires = _now() + dt.timedelta(days=config.REFRESH_TTL_D)
            new_refresh, new_jti = _store_refresh(conn, str(user_id), str(family_id), new_expires)
            conn.execute(
                "UPDATE refresh_token SET revoked_at = now(), replaced_by = %s WHERE jti = %s",
                (new_jti, jti),
            )
            issued = (str(user_id), role, new_refresh)

    if reuse_family is not None:
        log.error("检测到 refresh token 重放，已吊销 family=%s", reuse_family)
        raise TokenReuseError("检测到异常登录，请重新登录")

    assert issued is not None
    user_id_s, role_s, refresh_s = issued
    access, ttl = make_access(user_id_s, role_s)
    return {
        "access_token": access,
        "refresh_token": refresh_s,
        "token_type": "bearer",
        "expires_in": ttl,
    }


def revoke_refresh(raw_refresh: str) -> None:
    """登出：只吊销这一条（不动 family，避免影响用户其他设备）。"""
    with db.connect() as conn:
        conn.execute(
            "UPDATE refresh_token SET revoked_at = now() "
            "WHERE token_hash = %s AND revoked_at IS NULL",
            (sha256_hex(raw_refresh),),
        )


def revoke_all_for_user(user_id: str) -> int:
    """注销账号 / 强制下线：吊销该用户全部 refresh。"""
    with db.connect() as conn:
        cur = conn.cursor()
        cur.execute(
            "UPDATE refresh_token SET revoked_at = now() "
            "WHERE user_id = %s AND revoked_at IS NULL RETURNING jti",
            (user_id,),
        )
        return len(cur.fetchall())


# ============================================================
# 匿名身份（未登录也可用，登录后归并到真实账号）
# ============================================================
# 为什么不用前端自己传一个 anon_id：那样任何人都能填别人的 id 直接读走对方的
# 过敏史和聊天记录。匿名身份必须是服务端签发、带签名、不可猜的。
ANON_PREFIX = "anon:"
ANON_TTL_DAYS = 30


def make_anon() -> dict:
    """签发匿名身份（typ=anon）。有效期 30 天，够覆盖「先体验后注册」的窗口。"""
    anon_id = ANON_PREFIX + uuid.uuid4().hex
    now = _now()
    exp = now + dt.timedelta(days=ANON_TTL_DAYS)
    payload = {
        "sub": anon_id,
        "typ": "anon",
        "iss": config.JWT_ISSUER,
        "jti": uuid.uuid4().hex,
        "iat": int(now.timestamp()),
        "exp": int(exp.timestamp()),
    }
    return {
        "anon_id": anon_id,
        "anon_token": jwt.encode(payload, jwt_secret(), algorithm=config.JWT_ALG),
        "expires_in": ANON_TTL_DAYS * 86400,
    }


def decode_anon(token: str) -> dict:
    try:
        claims = jwt.decode(
            token,
            jwt_secret(),
            algorithms=[config.JWT_ALG],
            issuer=config.JWT_ISSUER,
            options={"require": ["exp", "sub", "iss"]},
        )
    except jwt.ExpiredSignatureError as exc:
        raise InvalidTokenError("匿名身份已过期") from exc
    except jwt.InvalidTokenError as exc:
        raise InvalidTokenError("令牌无效") from exc
    if claims.get("typ") != "anon":
        raise InvalidTokenError("令牌类型不正确")
    if not str(claims.get("sub", "")).startswith(ANON_PREFIX):
        # 只认自己签发的 anon 前缀，防止有人拿真实 user_id 伪装成匿名身份去做归并
        raise InvalidTokenError("匿名标识不合法")
    return claims
