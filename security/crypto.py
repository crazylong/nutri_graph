"""
security/crypto.py —— 手机号与令牌的加解密 / 哈希 / 脱敏

三条存储原则（对应 docs/plan-auth-and-dynamic-kg.md §1.3 第 4 项）：
  1. phone_hash  —— HMAC 摘要，承担唯一索引与查询，泄露也无法反推手机号
  2. phone_enc   —— Fernet 密文，只有服务端能还原（合规要求「可删除」时也靠它定位）
  3. phone_mask  —— 138****8888，列表页/日志直接展示，永远不出现明文

为什么不用明文 + 只靠权限控制：手机号属个人信息，饮食与疾病属敏感个人信息。
日志、备份、导出、SQL 报错任何一个环节泄露明文，都无法回收。
"""
from __future__ import annotations

import base64
import hashlib
import hmac
import logging
import secrets
import threading

import config

log = logging.getLogger("security.crypto")

_fernet = None
_lock = threading.Lock()
_pepper_warned = False
_ephemeral_secret: str | None = None


def _derive_key(secret: str, info: bytes) -> bytes:
    """HKDF-SHA256 派生固定长度的密钥材料。"""
    from cryptography.hazmat.primitives import hashes
    from cryptography.hazmat.primitives.kdf.hkdf import HKDF

    return HKDF(
        algorithm=hashes.SHA256(), length=32, salt=b"nutri_graph", info=info
    ).derive(secret.encode("utf-8"))


def _get_fernet():
    """Fernet 实例（惰性构建，避免导入期就要求配置齐全）。"""
    global _fernet
    if _fernet is not None:
        return _fernet
    from cryptography.fernet import Fernet

    with _lock:
        if _fernet is not None:
            return _fernet
        raw = config.PHONE_ENC_KEY.strip()
        if raw:
            key = raw.encode("utf-8")
        else:
            log.warning(
                "PHONE_ENC_KEY 未配置，暂由 JWT_SECRET 派生手机号加密密钥。"
                "生产必须显式配置独立的 PHONE_ENC_KEY，否则修改 JWT_SECRET 会导致历史手机号无法解密。"
            )
            key = base64.urlsafe_b64encode(_derive_key(_secret_fallback(), b"phone-enc"))
        _fernet = Fernet(key)
        return _fernet


def _secret_fallback() -> str:
    global _ephemeral_secret
    secret = config.JWT_SECRET.strip()
    if secret:
        return secret
    if _ephemeral_secret is None:
        # 最差情况：进程内一次性密钥。重启即丢失历史密文的可解密性。
        log.error(
            "JWT_SECRET 与 PHONE_ENC_KEY 都未配置，使用进程内临时密钥："
            "重启后已存手机号将无法解密（历史账号不可登录）。仅限临时调试。"
        )
        _ephemeral_secret = secrets.token_urlsafe(32)
    return _ephemeral_secret


def jwt_secret() -> str:
    """JWT 签名密钥（未配置时退化为进程内临时密钥，重启即全端登出）。"""
    return _secret_fallback()


def _pepper() -> str:
    global _pepper_warned
    p = config.PHONE_HASH_PEPPER.strip()
    if p:
        return p
    if not _pepper_warned:
        log.warning(
            "PHONE_HASH_PEPPER 未配置，暂由 JWT_SECRET 派生。"
            "注意：更换 pepper 会让所有已注册手机号查不到（等于全员无法登录）。"
        )
        _pepper_warned = True
    return "pepper:" + _secret_fallback()


# ============================================================
# 手机号
# ============================================================
def encrypt_phone(phone: str) -> str:
    return _get_fernet().encrypt(phone.encode("utf-8")).decode("ascii")


def decrypt_phone(enc: str) -> str:
    try:
        return _get_fernet().decrypt(enc.encode("ascii")).decode("utf-8")
    except Exception as exc:  # noqa: BLE001
        log.error("手机号解密失败（密钥变更？）：%s", exc)
        return ""


def phone_hash(phone: str) -> str:
    """手机号唯一索引键。加 pepper 是为了防止彩虹表直接枚举 11 位号码空间。"""
    return hmac.new(
        _pepper().encode("utf-8"), phone.encode("utf-8"), hashlib.sha256
    ).hexdigest()


def pepper() -> str:
    """给其他模块复用同一套 pepper（如验证码哈希），避免各写一套 fallback。"""
    return _pepper()


def phone_mask(phone: str) -> str:
    if len(phone) == 11:
        return f"{phone[:3]}****{phone[-4:]}"
    if len(phone) > 4:
        return phone[:2] + "*" * (len(phone) - 4) + phone[-2:]
    return "*" * len(phone)


# ============================================================
# 通用摘要
# ============================================================
def hmac_hex(secret: str, msg: str) -> str:
    return hmac.new(secret.encode("utf-8"), msg.encode("utf-8"), hashlib.sha256).hexdigest()


def sha256_hex(msg: str) -> str:
    return hashlib.sha256(msg.encode("utf-8")).hexdigest()


def new_token(nbytes: int = 32) -> str:
    return secrets.token_urlsafe(nbytes)


def new_numeric_code(length: int = 6) -> str:
    """生成数字验证码（仅 console provider 用；dypns 由平台生成）。"""
    length = max(4, min(8, length))
    return "".join(secrets.choice("0123456789") for _ in range(length))
