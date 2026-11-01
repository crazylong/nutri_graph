"""上海日历日。配额和费用都用这个口径，不用数据库会话时区。"""
from __future__ import annotations

from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

SHANGHAI = ZoneInfo("Asia/Shanghai")


def now() -> datetime:
    return datetime.now(SHANGHAI)


def today():
    return now().date()


def day_bounds() -> tuple[datetime, datetime]:
    start = now().replace(hour=0, minute=0, second=0, microsecond=0)
    return start, start + timedelta(days=1)


def seconds_until_midnight() -> int:
    current = now()
    end = (current + timedelta(days=1)).replace(hour=0, minute=0, second=0, microsecond=0)
    return max(1, int((end - current).total_seconds()))
