"""匿名身份与登录后归并"""
from __future__ import annotations

import psycopg

import config
from tests.helpers import bearer, login, requires_pg

pytestmark = requires_pg


def _q(sql: str, params: tuple):
    with psycopg.connect(config.PG_DSN) as conn:
        return conn.execute(sql, params).fetchall()


def test_anon_token_is_accepted_but_not_a_user(client):
    anon = client.post("/auth/anon").json()
    assert anon["anon_id"].startswith("anon:")

    r = client.get("/auth/principal", headers=bearer(anon["anon_token"]))
    assert r.status_code == 200
    assert r.json()["kind"] == "anon"

    # 匿名不是账号：需要登录的接口一律拒绝
    assert client.get("/auth/me", headers=bearer(anon["anon_token"])).status_code == 401


def test_merge_moves_sessions_and_profile(client):
    anon = client.post("/auth/anon").json()
    anon_id = anon["anon_id"]

    with psycopg.connect(config.PG_DSN) as conn:
        conn.execute(
            "INSERT INTO session (user_id, summary) VALUES (%s, %s)",
            (anon_id, "匿名期间问过海鲜"),
        )
        conn.execute(
            "INSERT INTO user_profile (user_id, allergies, avoid_list) VALUES (%s, %s, %s)",
            (anon_id, ["海鲜"], ["香菜"]),
        )

    tokens = login(client, anon_token=anon["anon_token"])
    real_id = tokens["user"]["user_id"]
    merged = tokens["merged"]
    assert merged["sessions"] == 1
    assert merged["profile"] is True

    # 会话挂到了真实账号下
    rows = _q("SELECT user_id, summary FROM session WHERE user_id = %s", (real_id,))
    assert len(rows) == 1 and rows[0][1] == "匿名期间问过海鲜"

    # 画像搬过来了，匿名行已删除
    row = _q("SELECT allergies, avoid_list FROM user_profile WHERE user_id = %s", (real_id,))
    assert row and "海鲜" in row[0][0] and "香菜" in row[0][1]
    assert _q("SELECT count(*) FROM user_profile WHERE user_id = %s", (anon_id,))[0][0] == 0


def test_merge_unions_with_existing_profile(client, monkeypatch):
    """目标账号已有画像时要取并集，不能覆盖掉原来的忌口。"""
    # 同一个号要连登两次，把发送间隔压到 0 才不会被频控挡住
    monkeypatch.setattr(config, "SMS_INTERVAL_S", 0)

    tokens = login(client)
    real_id = tokens["user"]["user_id"]
    with psycopg.connect(config.PG_DSN) as conn:
        conn.execute(
            "INSERT INTO user_profile (user_id, allergies, diet_style) VALUES (%s, %s, %s) "
            "ON CONFLICT (user_id) DO UPDATE SET allergies = EXCLUDED.allergies",
            (real_id, ["牛奶"], "清淡"),
        )

    anon = client.post("/auth/anon").json()
    with psycopg.connect(config.PG_DSN) as conn:
        conn.execute(
            "INSERT INTO user_profile (user_id, allergies) VALUES (%s, %s)",
            (anon["anon_id"], ["海鲜"]),
        )

    # 同一个手机号再登一次，带上匿名令牌
    tokens2 = login(client, phone=tokens["_phone"], anon_token=anon["anon_token"])
    assert tokens2["user"]["user_id"] == real_id

    row = _q("SELECT allergies, diet_style FROM user_profile WHERE user_id = %s", (real_id,))
    allergies, diet_style = row[0]
    assert "牛奶" in allergies and "海鲜" in allergies, "并集失败，原忌口被覆盖"
    assert diet_style == "清淡"


def test_merge_is_idempotent(client):
    anon = client.post("/auth/anon").json()
    with psycopg.connect(config.PG_DSN) as conn:
        conn.execute("INSERT INTO session (user_id) VALUES (%s)", (anon["anon_id"],))

    tokens = login(client, anon_token=anon["anon_token"])
    real_id = tokens["user"]["user_id"]
    assert tokens["merged"]["sessions"] == 1

    # 再归并一次：匿名名下已空，不应产生重复会话
    from memory.anonymous_merge import merge_anon_into_user

    again = merge_anon_into_user(anon["anon_id"], real_id)
    assert again["sessions"] == 0
    n = _q("SELECT count(*) FROM session WHERE user_id = %s", (real_id,))[0][0]
    assert n == 1


def test_invalid_anon_token_does_not_block_login(client):
    tokens = login(client, anon_token="这不是令牌")
    assert tokens["access_token"]
    assert tokens["merged"]["skipped"] is True


def test_merge_rejects_non_anon_id(client):
    """只认服务端签发的 anon: 前缀，防止把别人的 user_id 当匿名身份合并进来。"""
    from memory.anonymous_merge import merge_anon_into_user

    tokens = login(client)
    victim = login(client)
    result = merge_anon_into_user(victim["user"]["user_id"], tokens["user"]["user_id"])
    assert result["skipped"] is True
    assert result["sessions"] == 0
