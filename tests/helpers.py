"""tests/helpers.py —— 测试辅助（不走 fixture，直接函数调用更直观）"""
from __future__ import annotations

import secrets
import string

import pytest


def pg_reachable() -> bool:
    try:
        import psycopg

        import config

        with psycopg.connect(config.PG_DSN, connect_timeout=3) as conn:
            conn.execute("SELECT 1")
        return True
    except Exception:  # noqa: BLE001
        return False


PG_OK = pg_reachable()

# 本机没起 PostgreSQL 时整组跳过，而不是红一片
requires_pg = pytest.mark.skipif(
    not PG_OK, reason="PostgreSQL 不可达（设置 PG_DSN 后重跑）"
)


def neo4j_reachable() -> bool:
    try:
        from neo4j import GraphDatabase

        import config

        driver = GraphDatabase.driver(
            config.NEO4J_URI,
            auth=(config.NEO4J_USER, config.NEO4J_PASSWORD),
            connection_timeout=3,
        )
        with driver.session() as s:
            s.run("RETURN 1").consume()
        driver.close()
        return True
    except Exception:  # noqa: BLE001
        return False


NEO4J_OK = neo4j_reachable()

# 涉及主图写入/查询的用例需要 Neo4j；连不上就跳过，不要红
requires_neo4j = pytest.mark.skipif(
    not NEO4J_OK, reason="Neo4j 不可达（设置 NEO4J_URI 后重跑）"
)


def rand_phone() -> str:
    """每次生成不同手机号，避免频控/锁定状态在用例间串味。"""
    return "13" + "".join(secrets.choice(string.digits) for _ in range(9))


def send_code(client, phone: str, device_fp: str = "") -> str:
    """发验证码并返回 console provider 的 dev_code。"""
    r = client.post("/auth/sms", json={"phone": phone, "device_fp": device_fp})
    assert r.status_code == 200, r.text
    return r.json().get("dev_code", "")


def login(client, phone: str | None = None, anon_token: str = "") -> dict:
    """完整登录，返回 /auth/token 的响应体（附带 _phone 便于断言）。"""
    phone = phone or rand_phone()
    code = send_code(client, phone)
    assert code, "拿不到 dev_code：确认 DEBUG=true 且 SMS_PROVIDER=console"
    r = client.post(
        "/auth/token",
        json={"phone": phone, "code": code, "anon_token": anon_token},
    )
    assert r.status_code == 200, r.text
    body = r.json()
    body["_phone"] = phone
    return body


def bearer(token: str) -> dict:
    return {"Authorization": f"Bearer {token}"}


import contextlib


@contextlib.contextmanager
def hide_aliases(*alias_norms: str):
    """临时把若干个词的既有别名摘掉，退出时原样放回。

    为什么需要：部署环境的 dict_alias 里是有真实数据的（比如「黑豆」已经补录进主图）。
    而这几组用例的前提是「这个词还没被收录」/「词典里只有我插的那一条」。
    老写法靠「库是空的」来成立 —— 一旦有人真的用了这个系统，测试就开始红。

    只动指定的别名，且一定还原；不碰别的数据。
    """
    import psycopg

    import config

    norms = list(alias_norms)
    with psycopg.connect(config.PG_DSN) as conn:
        saved = conn.execute(
            "SELECT entity_type, canonical_id, alias, alias_norm, source, confidence "
            "FROM dict_alias WHERE alias_norm = ANY(%s)",
            (norms,),
        ).fetchall()
        conn.execute("DELETE FROM dict_alias WHERE alias_norm = ANY(%s)", (norms,))
    try:
        yield saved
    finally:
        if saved:
            with psycopg.connect(config.PG_DSN) as conn:
                with conn.cursor() as cur:
                    cur.executemany(
                        "INSERT INTO dict_alias "
                        "(entity_type, canonical_id, alias, alias_norm, source, confidence) "
                        "VALUES (%s,%s,%s,%s,%s,%s) ON CONFLICT DO NOTHING",
                        saved,
                    )
