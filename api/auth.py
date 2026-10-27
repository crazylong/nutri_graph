"""
api/auth.py —— 手机号验证码登录（needs 13）

流程：
  POST /auth/sms      发验证码（四维频控 → 通道下发 → 记账）
  POST /auth/token    验证码换令牌（失败锁定 → 校验 → 建号/登录 → 签发）
  POST /auth/refresh  刷新（旋转 + 重放检测）
  POST /auth/logout   登出（吊销当前 refresh）
  GET  /me            当前用户
  DELETE /me          注销（清 PII + 吊销全部令牌）

设计口径：
  * 注册与登录合并 —— 验证码登录，未注册即建号，不做「该手机号未注册」的枚举提示
  * 手机号只以 hash / 密文 / 掩码三种形态存在，任何响应与日志都不出现明文
  * 验证码的生成与校验交给通道（dypns 由平台托管，console 本地落库），
    但失败次数与锁定由本层自己记 —— 平台不代管刷单风险
"""
from __future__ import annotations

import logging
import re

import psycopg
from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel, Field

import config
from api.deps import client_ip, get_current_user, get_principal
from memory.anonymous_merge import merge_anon_into_user
from security import captcha, ratelimit
from security.crypto import encrypt_phone, phone_hash, phone_mask
from security.sms import get_provider
from security.tokens import (
    InvalidTokenError,
    TokenReuseError,
    decode_anon,
    issue_tokens,
    make_anon,
    revoke_all_for_user,
    revoke_refresh,
    rotate_refresh,
)

log = logging.getLogger("api.auth")

router = APIRouter(prefix="/auth", tags=["auth"])

_PHONE_RE = re.compile(r"^1[3-9]\d{9}$")


# ============================================================
# 入参
# ============================================================
class SmsRequest(BaseModel):
    phone: str = Field(..., description="中国大陆手机号")
    device_fp: str = Field("", max_length=128, description="设备指纹，可选")
    captcha_id: str = Field("", description="图形验证码 challenge_id，触发图形码时必填")
    captcha_answer: str = Field("", description="图形验证码答案")


class TokenRequest(BaseModel):
    phone: str
    code: str = Field(..., min_length=4, max_length=8)
    device_fp: str = ""
    policy_version: str = Field("", description="勾选的隐私政策版本，留空表示不记录")
    anon_token: str = Field(
        "", description="未登录期间拿到的匿名令牌；带上则把匿名数据归并到本账号"
    )


class RefreshRequest(BaseModel):
    refresh_token: str


class LogoutRequest(BaseModel):
    refresh_token: str


# ============================================================
# 工具
# ============================================================
def normalize_phone(raw: str) -> str:
    """去空格/连字符，容错 +86 / 0086 / 86 前缀。"""
    s = re.sub(r"[\s\-()]", "", raw or "")
    for prefix in ("+86", "0086"):
        if s.startswith(prefix):
            s = s[len(prefix):]
    if len(s) == 13 and s.startswith("86"):
        s = s[2:]
    return s


def _require_valid_phone(raw: str) -> str:
    phone = normalize_phone(raw)
    if not _PHONE_RE.match(phone):
        raise HTTPException(
            status_code=422,
            detail={"code": "invalid_phone", "message": "手机号格式不正确"},
        )
    return phone


def _upsert_user(phone: str) -> dict:
    """建号或登录。已注销的账号再次登录视为重新注册（PII 已在注销时清空）。"""
    ph = phone_hash(phone)
    enc = encrypt_phone(phone)
    mask = phone_mask(phone)
    with psycopg.connect(config.PG_DSN) as conn:
        cur = conn.cursor()
        cur.execute(
            "INSERT INTO app_user (phone_enc, phone_hash, phone_mask) VALUES (%s, %s, %s) "
            "ON CONFLICT (phone_hash) DO UPDATE SET "
            "  phone_enc = EXCLUDED.phone_enc,"
            "  phone_mask = EXCLUDED.phone_mask,"
            "  last_login_at = now(),"
            "  status = CASE WHEN app_user.status = 'deleted' THEN 'active' ELSE app_user.status END,"
            "  deleted_at = CASE WHEN app_user.status = 'deleted' THEN NULL ELSE app_user.deleted_at END "
            "RETURNING user_id, role, status",
            (enc, ph, mask),
        )
        user_id, role, status = cur.fetchone()

    if status != "active":
        raise HTTPException(
            status_code=403,
            detail={"code": "account_locked", "message": "账号已被限制，请联系管理员"},
        )
    return {"user_id": str(user_id), "role": role, "phone_mask": mask}


def _record_consent(user_id: str, version: str, ip: str) -> None:
    if not version:
        return
    try:
        with psycopg.connect(config.PG_DSN) as conn:
            conn.execute(
                "INSERT INTO user_consent (user_id, policy_version, ip) VALUES (%s, %s, %s) "
                "ON CONFLICT (user_id, policy_version) DO NOTHING",
                (user_id, version, ip),
            )
    except Exception as exc:  # noqa: BLE001 —— 同意记录失败不阻断登录，但要留痕
        log.error("记录隐私同意失败 user=%s version=%s：%s", user_id, version, exc)


def _too_many(dec: ratelimit.Decision) -> HTTPException:
    return HTTPException(
        status_code=429,
        detail={"code": dec.code, "message": dec.message},
        headers={"Retry-After": str(dec.retry_after)},
    )


# ============================================================
# 图形验证码
# ============================================================
@router.get("/captcha")
def get_captcha():
    """领取图形验证码。前端在「发码被要求验证码」时调用，返回 SVG 图片。"""
    c = captcha.generate()
    return {
        "challenge_id": c.challenge_id,
        "question": c.question,
        "svg": c.svg,
        "expires_in": c.expires_in,
    }


def _require_captcha_if_needed(req: SmsRequest, ip: str) -> None:
    """同一出口 IP 发送次数超过阈值后，强制校验图形验证码。

    首几次免图形码（降低正常用户摩擦），达到阈值后必须带且答对。
    注意：这里的判断在 check_send（频控）之前，让用户先过图形码这关，
    否则频控的 429 会先触发，图形码永远派不上用场。
    """
    sent = ratelimit.recent_sends_by_ip(ip)
    if sent < config.CAPTCHA_FREE_SENDS_PER_IP:
        return

    if not req.captcha_id:
        raise HTTPException(
            status_code=428,
            detail={"code": "captcha_required", "message": "请先完成图形验证码"},
        )
    ok, reason = captcha.verify(req.captcha_id, req.captcha_answer)
    if not ok:
        raise HTTPException(
            status_code=428,
            detail={"code": "captcha_invalid", "message": reason},
        )


# ============================================================
# 发验证码
# ============================================================
@router.post("/sms")
def send_sms(req: SmsRequest, request: Request):
    phone = _require_valid_phone(req.phone)
    ip = client_ip(request)
    ph = phone_hash(phone)

    _require_captcha_if_needed(req, ip)

    dec = ratelimit.check_send(ph, ip, req.device_fp)
    if not dec.allowed:
        raise _too_many(dec)

    provider = get_provider()
    result = provider.send(phone)

    ratelimit.record_send(
        phone_hash=ph,
        ip=ip,
        device_fp=req.device_fp,
        purpose="login",
        provider=provider.name,
        msg_id=result.msg_id,
        ok=result.ok,
        error=result.error,
    )

    if not result.ok:
        # 不把上游原文抛给用户（可能含内部标识），只记日志
        log.warning("短信下发失败 provider=%s error=%s", provider.name, result.error)
        raise HTTPException(
            status_code=502,
            detail={"code": "sms_send_failed", "message": "验证码发送失败，请稍后重试"},
        )

    body = {
        "ok": True,
        "provider": provider.name,
        "expires_in": config.SMS_VALID_TIME_S,
        "interval": config.SMS_INTERVAL_S,
        # 告诉前端：下次（同一 IP 再发）需要图形验证码了
        "captcha_required_next": ratelimit.recent_sends_by_ip(ip) >= config.CAPTCHA_FREE_SENDS_PER_IP,
    }
    # 仅本地联调回填；dypns 生产模式永远不回填
    if result.dev_code:
        body["dev_code"] = result.dev_code
    return body


# ============================================================
# 验证码换令牌
# ============================================================
@router.post("/token")
def exchange_token(req: TokenRequest, request: Request):
    phone = _require_valid_phone(req.phone)
    ip = client_ip(request)
    ph = phone_hash(phone)

    locked = ratelimit.check_verify_locked(ph, ip)
    if not locked.allowed:
        raise _too_many(locked)

    provider = get_provider()
    ok, reason = provider.verify(phone, req.code.strip())
    ratelimit.record_verify(ph, ip, "login", ok)

    if not ok:
        left = ratelimit.remaining_attempts(ph, ip)
        raise HTTPException(
            status_code=401,
            detail={
                "code": "invalid_code",
                "message": reason or "验证码不正确",
                "remaining_attempts": left,
            },
        )

    user = _upsert_user(phone)
    _record_consent(user["user_id"], req.policy_version, ip)
    tokens = issue_tokens(user["user_id"], user["role"])

    merged = None
    if req.anon_token:
        try:
            anon_claims = decode_anon(req.anon_token)
            merged = merge_anon_into_user(anon_claims["sub"], user["user_id"])
        except InvalidTokenError as exc:
            # 匿名令牌失效不影响登录本身，只放弃归并
            log.info("匿名令牌无效，跳过归并：%s", exc)
            merged = {"skipped": True, "errors": [str(exc)]}
        except Exception as exc:  # noqa: BLE001 —— 归并失败绝不能让登录失败
            log.error("匿名归并异常 user=%s：%s", user["user_id"], exc)
            merged = {"skipped": True, "errors": [str(exc)[:200]]}

    return {**tokens, "user": user, "merged": merged}


# ============================================================
# 刷新 / 登出
# ============================================================
@router.post("/refresh")
def refresh(req: RefreshRequest):
    try:
        return rotate_refresh(req.refresh_token)
    except TokenReuseError as exc:
        raise HTTPException(
            status_code=401,
            detail={"code": "token_reuse", "message": str(exc)},
        ) from exc
    except InvalidTokenError as exc:
        raise HTTPException(
            status_code=401,
            detail={"code": "invalid_refresh", "message": str(exc)},
        ) from exc


@router.post("/logout")
def logout(req: LogoutRequest):
    revoke_refresh(req.refresh_token)
    return {"ok": True}


# ============================================================
# 匿名身份
# ============================================================
@router.post("/anon")
def create_anon():
    """领取匿名身份：未登录也能聊天，登录时用 anon_token 把数据带过去。

    必须由服务端签发 —— 若让前端自己传一个 id，任何人填别人的 id
    就能把他的过敏史和聊天记录并进自己的账号。
    """
    return make_anon()


@router.get("/principal")
def principal(who: dict | None = Depends(get_principal)):
    """当前身份（用户或匿名）。前端用它判断要不要弹登录。"""
    if who is None:
        return {"kind": "none"}
    return who


# ============================================================
# 当前用户
# ============================================================
@router.get("/me")
def me(user: dict = Depends(get_current_user)):
    return {
        "user_id": user["user_id"],
        "phone_mask": user["phone_mask"],
        "role": user["role"],
    }


@router.delete("/me")
def delete_me(user: dict = Depends(get_current_user)):
    """注销：清 PII、吊销全部令牌、尽力删除历史对话与画像。

    手机号明文与掩码都清空，但保留 phone_hash —— 用于防「注销后立刻重新注册刷福利」
    类运营场景；再次登录会走 _upsert_user 的重新激活分支。
    """
    uid = user["user_id"]
    revoke_all_for_user(uid)
    purged = {}
    with psycopg.connect(config.PG_DSN) as conn:
        for table, sql in {
            "user_profile": "DELETE FROM user_profile WHERE user_id = %s",
            "memory_fact": "DELETE FROM memory_fact WHERE user_id = %s",
            "message": (
                "DELETE FROM message WHERE session_id IN "
                "(SELECT id FROM session WHERE user_id = %s)"
            ),
            "session": "DELETE FROM session WHERE user_id = %s",
            "user_consent": "DELETE FROM user_consent WHERE user_id = %s",
        }.items():
            try:
                # 每张表一个 savepoint：老表可能还没建（init_pg 未跑），
                # 失败只回滚这一条，不能把整个事务带成 aborted 状态。
                with conn.transaction():
                    cur = conn.cursor()
                    cur.execute(sql, (uid,))
                    purged[table] = cur.rowcount
            except Exception as exc:  # noqa: BLE001
                log.warning("注销清理 %s 失败（跳过）：%s", table, exc)
                purged[table] = -1
        conn.execute(
            "UPDATE app_user SET status = 'deleted', deleted_at = now(), "
            "phone_enc = '', phone_mask = '' WHERE user_id = %s",
            (uid,),
        )
    log.info("账号已注销 user=%s 清理明细=%s", uid, purged)
    return {"ok": True, "purged": purged}
