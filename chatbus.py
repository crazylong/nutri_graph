"""问答流式事件列表。测试用内存实现，线上用 Redis。"""
from __future__ import annotations

import asyncio
import json
import logging
import time

import config

log = logging.getLogger("chatbus")

TTL = 600


def stream_limit_seconds() -> float:
    """SSE 总时长：(候选数 + 兜底) × LLM_TIMEOUT + 120。"""
    from llm.router import fallback_spec, get_router

    count = len(get_router().candidates())
    if fallback_spec() is not None:
        count += 1
    return count * float(config.LLM_TIMEOUT) + 120


class MemoryBus:
    """测试用。接口与 RedisBus 相同，不连真 Redis。"""

    def __init__(self):
        self.lists: dict[str, list] = {}
        self.ttl: dict[str, int] = {}
        self.cancels: set[str] = set()
        self.fail = False

    def push(self, stream_id: str, event: str, data: dict | None = None) -> bool:
        if self.fail:
            return False
        key = f"nutri:chat:{stream_id}"
        self.lists.setdefault(key, []).append({"event": event, "data": data or {}})
        self.ttl[key] = TTL
        return True

    def exists(self, stream_id: str) -> bool:
        return f"nutri:chat:{stream_id}" in self.lists

    def delete(self, stream_id: str) -> None:
        key = f"nutri:chat:{stream_id}"
        self.lists.pop(key, None)
        self.ttl.pop(key, None)

    def cancel(self, stream_id: str) -> None:
        self.cancels.add(stream_id)

    def cancelled(self, stream_id: str) -> bool:
        return stream_id in self.cancels

    def events(self, stream_id: str) -> list:
        return list(self.lists.get(f"nutri:chat:{stream_id}", []))

    async def blpop(self, stream_id: str, timeout: float):
        key = f"nutri:chat:{stream_id}"
        if self.lists.get(key):
            return self.lists[key].pop(0)
        await asyncio.sleep(0)
        return None


class RedisBus:
    def __init__(self, client):
        self.client = client

    def push(self, stream_id: str, event: str, data: dict | None = None) -> bool:
        key = f"nutri:chat:{stream_id}"
        try:
            pipe = self.client.pipeline()
            pipe.rpush(key, json.dumps({"event": event, "data": data or {}}, ensure_ascii=False))
            pipe.expire(key, TTL)
            pipe.execute()
            return True
        except Exception as exc:  # noqa: BLE001
            log.warning("Redis RPUSH 失败：%s", exc)
            return False

    def exists(self, stream_id: str) -> bool:
        try:
            return bool(self.client.exists(f"nutri:chat:{stream_id}"))
        except Exception:  # noqa: BLE001
            return False

    def delete(self, stream_id: str) -> None:
        try:
            self.client.delete(f"nutri:chat:{stream_id}")
        except Exception as exc:  # noqa: BLE001
            log.warning("Redis DEL 失败：%s", exc)

    def cancel(self, stream_id: str) -> None:
        try:
            self.client.set(f"nutri:chat:{stream_id}:cancel", "1", ex=TTL)
        except Exception as exc:  # noqa: BLE001
            log.warning("Redis 取消标记失败：%s", exc)

    def cancelled(self, stream_id: str) -> bool:
        try:
            return bool(self.client.get(f"nutri:chat:{stream_id}:cancel"))
        except Exception:  # noqa: BLE001
            return False

    async def blpop(self, stream_id: str, timeout: float):
        import redis.asyncio as aioredis

        client = aioredis.Redis(
            host=config.REDIS_HOST,
            port=config.REDIS_PORT,
            password=config.REDIS_PASSWORD,
            decode_responses=True,
            socket_connect_timeout=2,
        )
        try:
            item = await client.blpop(f"nutri:chat:{stream_id}", timeout=timeout)
        except Exception as exc:  # noqa: BLE001
            log.warning("Redis BLPOP 失败：%s", exc)
            return None
        finally:
            await client.aclose()
        if not item:
            return None
        _key, raw = item
        return json.loads(raw)


def open_bus():
    """密码为空或连不上时返回 None。调用方在写出 SSE 头之前改回 503。"""
    if not config.REDIS_PASSWORD:
        return None
    try:
        import redis

        client = redis.Redis(
            host=config.REDIS_HOST,
            port=config.REDIS_PORT,
            password=config.REDIS_PASSWORD,
            decode_responses=True,
            socket_connect_timeout=2,
        )
        client.ping()
        return RedisBus(client)
    except Exception as exc:  # noqa: BLE001
        log.warning("Redis 不可用：%s", exc)
        return None


def _frame(event: str, data: dict) -> str:
    return f"event: {event}\ndata: {json.dumps(data, ensure_ascii=False)}\n\n"


async def iter_sse(bus, stream_id: str, deadline: float):
    """读到 done 之后才删列表。超时只结束这条 SSE，不取消工作线程。"""
    seen = False
    while time.monotonic() < deadline:
        left = deadline - time.monotonic()
        item = await bus.blpop(stream_id, timeout=min(15, max(left, 0.01)))
        if item is None:
            if bus.exists(stream_id):
                yield ": ping\n\n"
                continue
            if seen:
                yield _frame("error", {"text": "连接中断"})
                yield _frame("done", {})
                return
            yield ": ping\n\n"
            continue
        seen = True
        event = item.get("event") or "message"
        yield _frame(event, item.get("data") or {})
        if event == "done":
            bus.delete(stream_id)
            return
    yield _frame("error", {"text": "回答超时"})
    yield _frame("done", {})
