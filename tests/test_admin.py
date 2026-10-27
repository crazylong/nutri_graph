"""管理员授权面"""
from __future__ import annotations

import pytest

import config
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
