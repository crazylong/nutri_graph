"""Redis 探活失败时 /health 要指出来，缓存未到期不再探。"""
from __future__ import annotations

import api.health as health


def test_redis_down_is_named(client, monkeypatch):
    monkeypatch.setattr(health.config, "HEALTH_CACHE_TTL_S", 0)
    health._cached["body"] = None
    monkeypatch.setattr(health, "_check_redis", lambda: {"ok": False, "error": "down"})
    monkeypatch.setattr(health, "_check_neo4j", lambda: {"ok": True})
    monkeypatch.setattr(health, "_check_milvus", lambda: {"ok": True})
    monkeypatch.setattr(health, "_check_postgres", lambda: {"ok": True})
    response = client.get("/health")
    assert response.status_code == 503
    assert response.json()["checks"]["redis"]["ok"] is False


def test_health_reuses_unexpired_cache(client, monkeypatch):
    monkeypatch.setattr(health.config, "HEALTH_CACHE_TTL_S", 30)
    health._cached["body"] = None
    calls = {"n": 0}

    def neo():
        calls["n"] += 1
        return {"ok": True}

    monkeypatch.setattr(health, "_check_neo4j", neo)
    monkeypatch.setattr(health, "_check_milvus", lambda: {"ok": True})
    monkeypatch.setattr(health, "_check_postgres", lambda: {"ok": True})
    monkeypatch.setattr(health, "_check_redis", lambda: {"ok": True})
    assert client.get("/health").status_code == 200
    assert client.get("/health").status_code == 200
    assert calls["n"] == 1
