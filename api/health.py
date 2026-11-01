"""
api/health.py —— 存储探活

刻意做真实探测（真的连一次）而不是只返回 200：
docker-compose 起来后，一眼能看出是 Neo4j 没起还是 Milvus 没起。
"""
from __future__ import annotations

import time

from fastapi import APIRouter
from fastapi.responses import JSONResponse

import config

router = APIRouter(tags=["system"])


def _check_neo4j() -> dict:
    from neo4j import GraphDatabase

    try:
        driver = GraphDatabase.driver(
            config.NEO4J_URI,
            auth=(config.NEO4J_USER, config.NEO4J_PASSWORD),
        )
        with driver.session() as s:
            s.run("RETURN 1").single()
        driver.close()
        return {"ok": True}
    except Exception as e:  # noqa: BLE001
        return {"ok": False, "error": str(e)}


def _check_milvus() -> dict:
    from pymilvus import connections

    try:
        connections.connect(alias="health", uri=config.MILVUS_URI)
        connections.disconnect("health")
        return {"ok": True}
    except Exception as e:  # noqa: BLE001
        return {"ok": False, "error": str(e)}


def _check_redis() -> dict:
    try:
        import redis

        client = redis.Redis(
            host=config.REDIS_HOST,
            port=config.REDIS_PORT,
            password=config.REDIS_PASSWORD or None,
            socket_connect_timeout=2,
        )
        client.ping()
        return {"ok": True}
    except Exception as e:  # noqa: BLE001
        return {"ok": False, "error": str(e)}


def _check_postgres() -> dict:
    import psycopg

    try:
        with psycopg.connect(config.PG_DSN, connect_timeout=3) as conn:
            conn.execute("SELECT 1")
        return {"ok": True}
    except Exception as e:  # noqa: BLE001
        return {"ok": False, "error": str(e)}


_cached = {"at": 0.0, "status": 200, "body": None}


def _probe() -> tuple[int, dict]:
    checks = {
        "neo4j": _check_neo4j(),
        "milvus": _check_milvus(),
        "postgres": _check_postgres(),
        "redis": _check_redis(),
    }
    all_ok = all(v["ok"] for v in checks.values())
    status = 200 if all_ok else 503
    body = {
        "app": config.APP_NAME,
        "status": "ok" if all_ok else "degraded",
        "time": time.time(),
        "checks": checks,
    }
    return status, body


@router.get("/health")
def health():
    ttl = float(config.HEALTH_CACHE_TTL_S)
    now = time.monotonic()
    if ttl > 0 and _cached["body"] is not None and now - _cached["at"] < ttl:
        return JSONResponse(status_code=_cached["status"], content=_cached["body"])
    status, body = _probe()
    _cached["at"] = now
    _cached["status"] = status
    _cached["body"] = body
    return JSONResponse(status_code=status, content=body)
