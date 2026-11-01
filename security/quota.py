"""用户日配额和匿名领号日上限。超限时不把计数加上。"""
from __future__ import annotations

import logging

import config
from utils.clock import today

log = logging.getLogger("security.quota")


def _charge(table: str, column: str, key: str, limit: int) -> bool:
    if limit < 1 or not key:
        return False
    import db

    with db.connect() as conn:
        row = conn.execute(
            f"""
            INSERT INTO {table} ({column}, day, n) VALUES (%s, %s, 1)
            ON CONFLICT ({column}, day) DO UPDATE SET n = {table}.n + 1
            WHERE {table}.n < %s
            RETURNING n
            """,
            (key, today(), limit),
        ).fetchone()
    return row is not None


def charge_chat(user_key: str) -> bool:
    return _charge("chat_usage_day", "user_key", user_key, config.CHAT_QUOTA_PER_USER_DAY)


def charge_anon(ip: str) -> bool:
    return _charge("anon_issue_day", "ip", ip or "unknown", config.ANON_ISSUE_PER_IP_DAY)
