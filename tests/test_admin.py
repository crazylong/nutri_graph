"""管理员授权面"""
from __future__ import annotations

import secrets

import psycopg
import pytest
from psycopg.types.json import Jsonb

import config
from kg.request import register
from tests.helpers import bearer, login, requires_pg

pytestmark = requires_pg


def _promote(user_id: str) -> None:
    import psycopg

    with psycopg.connect(config.PG_DSN) as conn:
        conn.execute("UPDATE app_user SET role = 'admin' WHERE user_id = %s", (user_id,))


def test_normal_user_forbidden(client):
    tokens = login(client)
    r = client.get("/admin/stats", headers=bearer(tokens["access_token"]))
    assert r.status_code == 403
    assert r.json()["detail"]["code"] == "forbidden"


def test_anonymous_unauthorized(client):
    anon = client.post("/auth/anon").json()["anon_token"]
    r = client.get("/admin/stats", headers=bearer(anon))
    assert r.status_code == 401


def test_no_token_unauthorized(client):
    assert client.get("/admin/stats").status_code == 401


def test_admin_reads_stats(client):
    tokens = login(client)
    _promote(tokens["user"]["user_id"])
    r = client.get("/admin/stats", headers=bearer(tokens["access_token"]))
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["users"]["active"] >= 1
    assert body["sms"]["provider"] == config.SMS_PROVIDER
    assert body["sms"]["daily_quota"] == config.SMS_MAX_GLOBAL_DAY


def test_candidates_endpoint_degrades_cleanly(client):
    """kg_candidate 由 migrations/002_business.sql 建：没建时给 503 + 指引，不能 500。"""
    tokens = login(client)
    _promote(tokens["user"]["user_id"])
    r = client.get("/admin/kg/candidates", headers=bearer(tokens["access_token"]))
    assert r.status_code in (200, 503)
    if r.status_code == 503:
        assert r.json()["detail"]["code"] == "table_missing"
        assert "migrate" in r.json()["detail"]["message"]
    else:
        assert "items" in r.json()


@pytest.mark.parametrize("bad_status", ["unknown", "delete from users", "Pending"])
def test_candidates_status_validated(client, bad_status):
    tokens = login(client)
    _promote(tokens["user"]["user_id"])
    r = client.get(
        "/admin/kg/candidates",
        params={"status": bad_status},
        headers=bearer(tokens["access_token"]),
    )
    # 枚举校验挡住非法值（422），不会拼进 SQL
    assert r.status_code == 422


def _admin(client):
    tokens = login(client)
    _promote(tokens["user"]["user_id"])
    return tokens, bearer(tokens["access_token"])


def _food_name() -> str:
    """2–8 个汉字，避开别名表里的常见食材。

    字母表里特意不放「丁」：kg.request 的切分后缀表里有「丁」（肉丁/菜丁的丁），
    「测丙丙丁」会被切成「测丙丙」，于是 food_key != 输入名，用例随机失败。
    同理不放丝/片/块/泥/酱/末/馅 这些后缀字。
    """
    alphabet = "甲乙丙戊己庚辛壬癸"
    return "测" + "".join(secrets.choice(alphabet) for _ in range(3))


def _delete_request(food_key: str) -> None:
    with psycopg.connect(config.PG_DSN) as conn:
        conn.execute("DELETE FROM kg_ingredient_request WHERE food_key = %s", (food_key,))


def test_console_login_becomes_admin(client):
    from tests.helpers import rand_phone, send_code

    phone = rand_phone()
    code = send_code(client, phone)
    r = client.post("/auth/token", json={"phone": phone, "code": code, "console": True})
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["user"]["role"] == "admin"
    headers = bearer(body["access_token"])
    me = client.get("/auth/me", headers=headers)
    assert me.status_code == 200
    assert me.json()["role"] == "admin"
    assert client.get("/admin/stats", headers=headers).status_code == 200


def test_normal_user_cannot_list_requests(client):
    tokens = login(client)
    r = client.get("/admin/kg/requests", headers=bearer(tokens["access_token"]))
    assert r.status_code == 403


def test_admin_register_skips_user_daily_limit(client, monkeypatch):
    monkeypatch.setattr(config, "FOOD_REQUEST_DAILY_LIMIT_PER_USER", 1)
    tokens, headers = _admin(client)
    first, second, third = _food_name(), _food_name(), _food_name()
    try:
        created = client.post("/admin/kg/requests", json={"name": first}, headers=headers)
        assert created.status_code == 200, created.text
        body = created.json()
        assert body["food_key"] == first
        assert body["status"] == "queued"
        assert body["hit_count"] == 0

        again = client.post("/admin/kg/requests", json={"name": second}, headers=headers)
        assert again.status_code == 200, again.text

        with psycopg.connect(config.PG_DSN) as conn:
            source = conn.execute(
                "SELECT source FROM kg_ingredient_request WHERE food_key = %s", (first,)
            ).fetchone()[0]
            task = conn.execute(
                "SELECT status FROM kg_fetch_task WHERE food_key = %s", (first,)
            ).fetchone()
        assert source == "admin"
        assert task is not None and task[0] == "queued"

        blocked = register(third, who=tokens["user"]["user_id"], source="chat")
        assert blocked.code == "user_daily_limit"
    finally:
        _delete_request(first)
        _delete_request(second)
        _delete_request(third)


def test_admin_register_repeat_keeps_source_and_status(client):
    _, headers = _admin(client)
    name = _food_name()
    try:
        created = client.post("/admin/kg/requests", json={"name": name}, headers=headers)
        assert created.status_code == 200, created.text
        request_id = created.json()["id"]
        rejected = client.post(
            f"/admin/kg/requests/{request_id}/reject",
            json={"reason": "不是要补的食物"},
            headers=headers,
        )
        assert rejected.status_code == 200, rejected.text
        assert rejected.json()["reject_reason"] == "不是要补的食物"

        again = client.post("/admin/kg/requests", json={"name": name}, headers=headers)
        assert again.status_code == 200, again.text
        assert again.json()["id"] == request_id
        assert again.json()["hit_count"] == 0
        assert again.json()["status"] == "rejected"

        listed = client.get(
            "/admin/kg/requests", params={"keyword": name}, headers=headers
        )
        assert listed.status_code == 200, listed.text
        row = listed.json()["items"][0]
        assert row["source"] == "admin"
        assert row["reject_reason"] == "不是要补的食物"
        assert row["status"] == "rejected"
    finally:
        _delete_request(name)


def test_admin_register_does_not_bump_search_count(client):
    """前台重复提问累加次数；管理员再登记同一食物名时次数不动。"""
    _, headers = _admin(client)
    asked = _food_name()
    queued = _food_name()
    try:
        first = register(asked, who="u-search", source="chat")
        assert first.hit_count == 1
        again = register(asked, who="u-search", source="chat")
        assert again.hit_count == 2

        touched = client.post("/admin/kg/requests", json={"name": asked}, headers=headers)
        assert touched.status_code == 200, touched.text
        assert touched.json()["id"] == first.request_id
        assert touched.json()["hit_count"] == 2

        created = client.post("/admin/kg/requests", json={"name": queued}, headers=headers)
        assert created.status_code == 200, created.text
        assert created.json()["hit_count"] == 0

        searched = register(queued, who="u-search", source="chat")
        assert searched.hit_count == 1
        assert searched.request_id == created.json()["id"]

        with psycopg.connect(config.PG_DSN) as conn:
            asked_row = conn.execute(
                "SELECT hit_count, source FROM kg_ingredient_request WHERE food_key = %s",
                (asked,),
            ).fetchone()
            queued_row = conn.execute(
                "SELECT hit_count, source FROM kg_ingredient_request WHERE food_key = %s",
                (queued,),
            ).fetchone()
        assert asked_row == (2, "chat")
        assert queued_row == (1, "admin")
    finally:
        _delete_request(asked)
        _delete_request(queued)


def test_admin_register_global_limit_is_an_error(client, monkeypatch):
    monkeypatch.setattr(config, "FOOD_REQUEST_GLOBAL_DAILY_LIMIT", 0)
    _, headers = _admin(client)
    name = _food_name()
    r = client.post("/admin/kg/requests", json={"name": name}, headers=headers)
    assert r.status_code == 429, r.text
    assert r.json()["detail"]["code"] == "global_daily_limit"
    with psycopg.connect(config.PG_DSN) as conn:
        row = conn.execute(
            "SELECT 1 FROM kg_ingredient_request WHERE food_key = %s", (name,)
        ).fetchone()
    assert row is None


def test_alias_candidates_are_read_only_list(client):
    _, headers = _admin(client)
    alias = "测" + _food_name()[1:]
    with psycopg.connect(config.PG_DSN) as conn:
        conn.execute(
            "INSERT INTO dict_alias_candidate "
            "(entity_type, alias, alias_norm, suggested_canonical_id, status) "
            "VALUES ('Ingredient', %s, %s, 'ING_TEST_ADMIN', 'approved') "
            "ON CONFLICT (entity_type, alias_norm) DO UPDATE SET status = 'approved'",
            (alias, alias),
        )
    try:
        r = client.get(
            "/admin/kg/alias-candidates",
            params={"status": "approved"},
            headers=headers,
        )
        assert r.status_code == 200, r.text
        matched = [row for row in r.json()["items"] if row["alias"] == alias]
        assert matched and matched[0]["status"] == "approved"
    finally:
        with psycopg.connect(config.PG_DSN) as conn:
            conn.execute(
                "DELETE FROM dict_alias_candidate WHERE alias_norm = %s", (alias,)
            )


def test_candidate_list_includes_sources(client):
    _, headers = _admin(client)
    candidate_id = None
    with psycopg.connect(config.PG_DSN) as conn:
        if conn.execute("SELECT to_regclass('public.kg_candidate')").fetchone()[0] is None:
            pytest.skip("kg_candidate 未建")
        src_id = "SRC_" + secrets.token_hex(4)
        row = conn.execute(
            "INSERT INTO kg_candidate "
            "(batch_id, src_type, src_id, rel, dst_type, dst_id, sources) "
            "VALUES ('admin-test', 'Ingredient', %s, 'CONTAINS_NUTRIENT', "
            "'Nutrient', 'DST_ADMIN_TEST', %s) "
            "RETURNING id",
            (src_id, Jsonb(["unit-test"])),
        ).fetchone()
        candidate_id = row[0]
    try:
        r = client.get(
            "/admin/kg/candidates", params={"status": "pending", "limit": 200}, headers=headers
        )
        assert r.status_code == 200, r.text
        matched = [item for item in r.json()["items"] if item["id"] == candidate_id]
        assert matched and matched[0]["sources"] == ["unit-test"]
    finally:
        if candidate_id is not None:
            with psycopg.connect(config.PG_DSN) as conn:
                conn.execute("DELETE FROM kg_candidate WHERE id = %s", (candidate_id,))
