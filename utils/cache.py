"""embedding 与单次图查询缓存。Redis 不可用时一律当未命中。"""
from __future__ import annotations

import hashlib
import json
import logging

import config

log = logging.getLogger("utils.cache")

_redis = None
_redis_failed = False


def _connect():
    global _redis, _redis_failed
    if _redis_failed:
        return None
    if _redis is not None:
        return _redis
    try:
        import redis

        client = redis.Redis(
            host=config.REDIS_HOST,
            port=config.REDIS_PORT,
            password=config.REDIS_PASSWORD or None,
            decode_responses=True,
            socket_connect_timeout=2,
        )
        client.ping()
        _redis = client
        return client
    except Exception as exc:  # noqa: BLE001
        _redis_failed = True
        log.warning("缓存 Redis 不可用，按未命中处理：%s", exc)
        return None


def reset_client() -> None:
    """测试里换假客户端之前清掉失败标记。"""
    global _redis, _redis_failed
    _redis = None
    _redis_failed = False


def allergen_hash(allergens: list[str] | None) -> str:
    items = sorted({str(a) for a in (allergens or []) if str(a)})
    return hashlib.sha1("|".join(items).encode("utf-8")).hexdigest()[:12]


def graph_key(intent: str, cid: str, allergens: list[str] | None, policy: str, include_prov: bool) -> str:
    epoch = "0"
    client = _connect()
    if client is not None:
        try:
            epoch = str(client.get("ng:graph:epoch") or "0")
        except Exception:  # noqa: BLE001
            epoch = "0"
    return (
        f"ng:graph:{intent}:{cid}:{allergen_hash(allergens)}:{policy}:"
        f"{int(bool(include_prov))}:{epoch}"
    )


def bump_graph_epoch() -> None:
    client = _connect()
    if client is None:
        return
    try:
        client.incr("ng:graph:epoch")
    except Exception as exc:  # noqa: BLE001
        log.warning("图缓存 epoch 自增失败：%s", exc)


def cached_embedding(model: str, text: str, loader):
    if not config.CACHE_ENABLED:
        return loader()
    from kg.normalize import normalize_text

    norm = normalize_text(text or "")
    digest = hashlib.sha1(norm.encode("utf-8")).hexdigest()
    key = f"ng:emb:{model}:{digest}"
    client = _connect()
    if client is None:
        return loader()
    try:
        hit = client.get(key)
        if hit:
            return json.loads(hit)
    except Exception as exc:  # noqa: BLE001
        log.warning("读 embedding 缓存失败：%s", exc)
        return loader()
    vec = loader()
    try:
        client.setex(key, int(config.CACHE_EMBEDDING_TTL_H) * 3600, json.dumps(vec))
    except Exception as exc:  # noqa: BLE001
        log.warning("写 embedding 缓存失败：%s", exc)
    return vec


def cached_json(key: str, ttl_s: int, loader):
    if not config.CACHE_ENABLED:
        return loader()
    client = _connect()
    if client is None:
        return loader()
    try:
        hit = client.get(key)
        if hit is not None:
            return json.loads(hit)
    except Exception as exc:  # noqa: BLE001
        log.warning("读缓存失败：%s", exc)
        return loader()
    value = loader()
    try:
        payload = json.dumps(value, ensure_ascii=False, default=str)
        client.setex(key, ttl_s, payload)
        return json.loads(payload)
    except Exception as exc:  # noqa: BLE001
        log.warning("写缓存失败：%s", exc)
        return value
