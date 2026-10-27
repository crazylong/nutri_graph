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


def _check_postgres() -> dict:
    import psycopg

    try:
        with psycopg.connect(config.PG_DSN, connect_timeout=3) as conn:
            conn.execute("SELECT 1")
        return {"ok": True}
    except Exception as e:  # noqa: BLE001
        return {"ok": False, "error": str(e)}


@router.get("/health")
def health():
    checks = {
        "neo4j": _check_neo4j(),
        "milvus": _check_milvus(),
        "postgres": _check_postgres(),
    }
    all_ok = all(v["ok"] for v in checks.values())
    return JSONResponse(
        status_code=200 if all_ok else 503,
        content={
            "app": config.APP_NAME,
            "status": "ok" if all_ok else "degraded",
            "time": time.time(),
            "checks": checks,
        },
    )
