"""
security/ratelimit.py —— 短信频控与验证码失败锁定

为什么必须自己实现（哪怕平台也有 Interval 参数）：
  平台只管「验证码本身」的安全（生成、有效期、同号重发兜底），
  不管「有人拿你的账号去刷别人手机号」的短信轰炸 —— 短信费还是你出。
  换句话说，平台防的是「验证码被猜中」，我们防的是「钱包被刷空」。

四维 + 熔断：
  手机号 60s/次、5 次/小时、10 次/天
  IP 10 次/小时
  设备指纹 10 次/天
  全局 200 次/天（超限即熔断，次日恢复）

频控仍用 sms_send_log 做窗口计数，不改用 Redis。
问答流式另有 Redis 列表，和这里的发送额度无关。
"""
from __future__ import annotations

import logging
from dataclasses import dataclass

import psycopg

import config

import db

log = logging.getLogger("security.ratelimit")


@dataclass
class Decision:
    allowed: bool
    code: str = ""          # 给前端区分的机器码：too_frequent / quota_exceeded / locked / global_limit
    message: str = ""
    retry_after: int = 0    # 秒，配合 HTTP 429 + Retry-After

    @classmethod
    def ok(cls) -> "Decision":
        return cls(allowed=True)



def _scalar(conn, sql: str, params: tuple) -> float:
    cur = conn.cursor()
    cur.execute(sql, params)
    row = cur.fetchone()
    return float(row[0]) if row and row[0] is not None else 0.0


# ============================================================
# 发送前检查
# ============================================================
def check_send(phone_hash: str, ip: str, device_fp: str) -> Decision:
    with db.connect() as conn:
        # 1) 同号最小间隔
        elapsed = _scalar(
            conn,
            "SELECT COALESCE(EXTRACT(EPOCH FROM (now() - max(created_at))), 1e6) "
            "FROM sms_send_log WHERE phone_hash = %s AND ok",
            (phone_hash,),
        )
        if elapsed < config.SMS_INTERVAL_S:
            wait = int(config.SMS_INTERVAL_S - elapsed) + 1
            return Decision(
                False, "too_frequent",
                f"发送过于频繁，请 {wait} 秒后再试", wait,
            )

        # 2) 同号小时 / 天配额
        n_hour = _scalar(
            conn,
            "SELECT count(*) FROM sms_send_log WHERE phone_hash = %s AND ok "
            "AND created_at > now() - interval '1 hour'",
            (phone_hash,),
        )
        if n_hour >= config.SMS_MAX_PER_PHONE_HOUR:
            return Decision(False, "quota_exceeded", "该手机号 1 小时内发送次数已达上限，请稍后再试", 3600)

        n_day = _scalar(
            conn,
            "SELECT count(*) FROM sms_send_log WHERE phone_hash = %s AND ok "
            "AND created_at > now() - interval '1 day'",
            (phone_hash,),
        )
        if n_day >= config.SMS_MAX_PER_PHONE_DAY:
            return Decision(False, "quota_exceeded", "该手机号今日发送次数已达上限，请明天再试", 86400)

        # 3) IP 维度（挡「换号刷同一入口」）
        if ip:
            n_ip = _scalar(
                conn,
                "SELECT count(*) FROM sms_send_log WHERE ip = %s AND ok "
                "AND created_at > now() - interval '1 hour'",
                (ip,),
            )
            if n_ip >= config.SMS_MAX_PER_IP_HOUR:
                return Decision(False, "quota_exceeded", "当前网络环境请求过于频繁，请稍后再试", 3600)

        # 4) 设备维度（挡接码平台批量换号）
        if device_fp:
            n_dev = _scalar(
                conn,
                "SELECT count(*) FROM sms_send_log WHERE device_fp = %s AND ok "
                "AND created_at > now() - interval '1 day'",
                (device_fp,),
            )
            if n_dev >= config.SMS_MAX_PER_DEVICE_DAY:
                return Decision(False, "quota_exceeded", "此设备今日发送次数已达上限", 86400)

        # 5) 全局熔断（防被刷爆账单）
        n_all = _scalar(
            conn,
            "SELECT count(*) FROM sms_send_log WHERE ok AND created_at > now() - interval '1 day'",
            (),
        )
        if n_all >= config.SMS_MAX_GLOBAL_DAY:
            log.error(
                "短信全局熔断触发：今日已发送 %d 条（上限 %d）。"
                "若不是正常业务量，请立刻检查是否被刷。",
                int(n_all), config.SMS_MAX_GLOBAL_DAY,
            )
            return Decision(False, "global_limit", "系统今日短信额度已用尽，请明日再试或联系管理员", 86400)

    return Decision.ok()


# ============================================================
# 校验前检查（连续失败锁定）
# ============================================================
_FAILS_SQL = (
    "SELECT count(*) FROM ("
    "  SELECT ok FROM sms_verify_attempt"
    "  WHERE {where} AND created_at > now() - (%s || ' minutes')::interval"
    "  ORDER BY id DESC LIMIT %s"
    ") t WHERE t.ok = false"
)


def _recent_fails(conn, where: str, where_params: tuple, limit: int) -> int:
    cur = conn.cursor()
    cur.execute(
        _FAILS_SQL.format(where=where),
        (*where_params, config.SMS_LOCK_MINUTES, limit),
    )
    row = cur.fetchone()
    return int(row[0]) if row and row[0] is not None else 0


def check_verify_locked(phone_hash: str, ip: str) -> Decision:
    """两个维度分别判，阈值不同：

    手机号 5 次 → 锁这个号（正常用户的自我保护）
    IP 50 次     → 才认为整个出口在爆破（避免 NAT 后面互相牵连）
    """
    with db.connect() as conn:
        phone_fails = _recent_fails(
            conn, "phone_hash = %s", (phone_hash,), config.SMS_MAX_VERIFY_FAILS
        )
        if phone_fails >= config.SMS_MAX_VERIFY_FAILS:
            return Decision(
                False, "locked",
                f"验证码错误次数过多，请 {config.SMS_LOCK_MINUTES} 分钟后再试",
                config.SMS_LOCK_MINUTES * 60,
            )

        if ip:
            ip_fails = _recent_fails(
                conn, "ip = %s", (ip,), config.SMS_MAX_VERIFY_FAILS_PER_IP
            )
            if ip_fails >= config.SMS_MAX_VERIFY_FAILS_PER_IP:
                log.warning(
                    "来源 IP %s 在 %d 分钟内验证码失败 %d 次，疑似批量爆破，暂时拦截",
                    ip, config.SMS_LOCK_MINUTES, ip_fails,
                )
                return Decision(
                    False, "locked",
                    f"当前网络环境验证异常，请 {config.SMS_LOCK_MINUTES} 分钟后再试",
                    config.SMS_LOCK_MINUTES * 60,
                )
    return Decision.ok()


def remaining_attempts(phone_hash: str, ip: str = "") -> int:
    """剩余可试次数只按手机号算 —— 用户看到的「还能试几次」应该只跟自己这个号有关。"""
    with db.connect() as conn:
        fails = _recent_fails(
            conn, "phone_hash = %s", (phone_hash,), config.SMS_MAX_VERIFY_FAILS
        )
    return max(0, config.SMS_MAX_VERIFY_FAILS - fails)


# ============================================================
# 记账
# ============================================================
def record_send(
    phone_hash: str,
    ip: str,
    device_fp: str,
    purpose: str,
    provider: str,
    msg_id: str = "",
    ok: bool = True,
    error: str = "",
    phone_mask: str = "",
    biz_id: str = "",
    delivery_status: str = "none",
) -> None:
    """每次下发都记一条（成功与否都记），失败也计入频控可防止「靠失败无限重试」。"""
    try:
        with db.connect() as conn:
            conn.execute(
                "INSERT INTO sms_send_log "
                "(phone_hash, ip, device_fp, purpose, provider, provider_msg_id, ok, error, "
                " phone_mask, biz_id, delivery_status) "
                "VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)",
                (
                    phone_hash, ip, device_fp, purpose, provider, msg_id, ok, error[:500],
                    phone_mask, biz_id, delivery_status,
                ),
            )
    except Exception as exc:  # noqa: BLE001 —— 记账失败不能阻断主流程
        log.error("写入 sms_send_log 失败：%s", exc)


def recent_sends_by_ip(ip: str, window_hours: int = 1) -> int:
    """某 IP 在窗口内的发送次数（含失败），用于判断是否需要图形验证码。"""
    if not ip:
        return 0
    with db.connect() as conn:
        return int(
            _scalar(
                conn,
                "SELECT count(*) FROM sms_send_log WHERE ip = %s "
                "AND created_at > now() - (%s || ' hours')::interval",
                (ip, window_hours),
            )
        )


def record_verify(phone_hash: str, ip: str, purpose: str, ok: bool) -> None:
    try:
        with db.connect() as conn:
            conn.execute(
                "INSERT INTO sms_verify_attempt (phone_hash, ip, purpose, ok) "
                "VALUES (%s, %s, %s, %s)",
                (phone_hash, ip, purpose, ok),
            )
    except Exception as exc:  # noqa: BLE001
        log.error("写入 sms_verify_attempt 失败：%s", exc)
