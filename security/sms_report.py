"""号码认证 HTTP 回执：按 out_id / biz_id 更新一行，不插新行。"""
from __future__ import annotations

import logging
import re
from datetime import datetime
from zoneinfo import ZoneInfo

import db

log = logging.getLogger("security.sms_report")

_OUT_ID = re.compile(r"^login-[0-9a-f]{12}$")
_SHANGHAI = ZoneInfo("Asia/Shanghai")


def parse_report_time(raw) -> datetime | None:
    text = str(raw or "").strip()
    if not text:
        return None
    try:
        naive = datetime.strptime(text, "%Y-%m-%d %H:%M:%S")
    except ValueError:
        return None
    return naive.replace(tzinfo=_SHANGHAI)


def parse_sms_size(raw) -> int | None:
    text = str(raw).strip() if raw is not None else ""
    if not text:
        return None
    try:
        return int(text)
    except ValueError:
        return None


def _delivered(value) -> bool:
    if isinstance(value, bool):
        return value
    return str(value or "").strip().lower() in {"true", "1", "yes"}


def _update(conn, column: str, key: str, fields: tuple) -> bool:
    if not key:
        return False
    cur = conn.execute(
        f"""
        UPDATE sms_send_log SET
            delivery_status = %s,
            err_code = %s,
            err_msg = %s,
            sms_size = %s,
            report_at = %s
        WHERE id = (
            SELECT id FROM sms_send_log
            WHERE {column} = %s
            ORDER BY id DESC
            LIMIT 1
        )
        """,
        (*fields, key),
    )
    return cur.rowcount == 1


def apply_item(item: dict) -> str:
    """返回 updated / sample / missing。missing 表示像我们的 out_id，但两把钥匙都对不上。"""
    out_id = str(item.get("out_id") or "").strip()
    biz_id = str(item.get("biz_id") or "").strip()
    fields = (
        "delivered" if _delivered(item.get("success")) else "failed",
        str(item.get("err_code") or "")[:64],
        str(item.get("err_msg") or "")[:200],
        parse_sms_size(item.get("sms_size")),
        parse_report_time(item.get("report_time")),
    )
    own = bool(_OUT_ID.match(out_id))
    with db.connect() as conn:
        if own and _update(conn, "provider_msg_id", out_id, fields):
            log.info("短信回执 out_id=%s biz_id=%s success=%s", out_id, biz_id, item.get("success"))
            return "updated"
        if biz_id and _update(conn, "biz_id", biz_id, fields):
            log.info("短信回执 out_id=%s biz_id=%s success=%s", out_id, biz_id, item.get("success"))
            return "updated"
    if own:
        log.warning("短信回执对不上 out_id=%s biz_id=%s", out_id, biz_id)
        return "missing"
    log.warning("短信回执未识别 out_id=%s biz_id=%s", out_id, biz_id)
    return "sample"
