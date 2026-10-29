"""未收录登记的反查、挂别名、自建营养素和抓取出口。"""
from __future__ import annotations

import secrets
import string

import psycopg

import config
from kg.draft import DraftError, FoodDraft
from kg.fetch import process_task
from kg.lookup import match
from kg.normalize import AliasStore
from kg.request import extract_candidates
from kg.synonym import suggest
from scripts.clean_request_noise import is_noise
from tests.helpers import bearer, login, requires_pg

pytestmark = requires_pg


def _token(n: int = 6) -> str:
    # 小写：录入时 name_key 会把拉丁字母转成小写，归一名必须和它一致
    alphabet = string.ascii_lowercase
    return "".join(secrets.choice(alphabet) for _ in range(n))


def _admin(client):
    tokens = login(client)
    with psycopg.connect(config.PG_DSN) as conn:
        conn.execute(
            "UPDATE app_user SET role = 'admin' WHERE user_id = %s",
            (tokens["user"]["user_id"],),
        )
    return bearer(tokens["access_token"])


def _seed(food_key: str, hit_count: int = 1, raw: str | None = None) -> int:
    raw = raw if raw is not None else food_key
    with psycopg.connect(config.PG_DSN) as conn:
        request_id = conn.execute(
            "INSERT INTO kg_ingredient_request "
            "(food_key, raw_mention, normalized, requested_by, source, status, hit_count) "
            "VALUES (%s, %s, %s, 'tester', 'chat', 'queued', %s) RETURNING id",
            (food_key, raw, food_key, hit_count),
        ).fetchone()[0]
        conn.execute(
            "INSERT INTO kg_fetch_task (request_id, food_key) VALUES (%s, %s)",
            (request_id, food_key),
        )
    return request_id


def _cleanup(food_key: str, alias_norm: str | None = None) -> None:
    with psycopg.connect(config.PG_DSN) as conn:
        conn.execute("DELETE FROM kg_food_bundle WHERE food_key = %s", (food_key,))
        if alias_norm:
            conn.execute(
                "DELETE FROM dict_alias WHERE entity_type = 'Ingredient' AND alias_norm = %s",
                (alias_norm,),
            )
            conn.execute(
                "DELETE FROM dict_alias_candidate WHERE entity_type = 'Ingredient' AND alias_norm = %s",
                (alias_norm,),
            )
        row = conn.execute(
            "SELECT id FROM kg_ingredient_request WHERE food_key = %s",
            (food_key,),
        ).fetchone()
        if row:
            conn.execute("DELETE FROM kg_request_action WHERE request_id = %s", (row[0],))
            conn.execute("DELETE FROM kg_ingredient_request WHERE id = %s", (row[0],))


def _bundle_body(**over):
    body = {
        "category": "其他",
        "energy_kcal": "46",
        "protein_g": "1.4",
        "fat_g": "0.7",
        "carb_g": "8.4",
    }
    body.update(over)
    return body


def test_extract_strips_new_break_words():
    assert extract_candidates("章鱼营养价值") == ["章鱼"]
    assert extract_candidates("奇亚籽每天") == ["奇亚籽"]
    assert extract_candidates("黑豆和什么一起吃好") == ["黑豆"]


def test_noise_predicate_spares_real_food_names():
    assert is_noise("章鱼营养价值")
    assert is_noise("奇亚籽每天")
    assert is_noise("热量")
    assert not is_noise("多宝鱼")


def test_synonym_is_off():
    assert suggest("猪脚") is None


def test_synonym_seed_comes_before_fuzzy(monkeypatch):
    def exact(self, alias_norm, entity_type=None):
        if alias_norm == "猪蹄":
            return [{
                "entity_type": "Ingredient",
                "canonical_id": "ING_TROT",
                "alias": "猪蹄",
                "confidence": 1,
            }]
        return []

    def fuzzy(self, term, entity_type=None, min_sim=0.62, limit=3):
        return [{
            "entity_type": "Ingredient",
            "canonical_id": "ING_TENDON",
            "alias": "猪脚筋",
            "sim": 0.6667,
        }]

    monkeypatch.setattr(AliasStore, "exact_lookup", exact)
    monkeypatch.setattr(AliasStore, "fuzzy_lookup", fuzzy)
    monkeypatch.setattr("kg.lookup.graph_by_name", lambda name: [])

    found = match("猪脚")
    assert found["verdict"] == "likely_exists"
    assert found["hits"][0]["layer"] == "synonym"
    assert found["hits"][0]["name"] == "猪蹄"
    assert found["hits"][0]["canonical_id"] == "ING_TROT"
    assert found["hits"][1]["layer"] == "fuzzy"


def test_unknown_term_is_not_found(monkeypatch):
    monkeypatch.setattr(AliasStore, "exact_lookup", lambda self, alias_norm, entity_type=None: [])
    monkeypatch.setattr(AliasStore, "fuzzy_lookup", lambda self, term, entity_type=None, min_sim=0.62, limit=3: [])
    monkeypatch.setattr("kg.lookup.graph_by_name", lambda name: [])
    found = match("天麻")
    assert found["verdict"] == "not_found"
    assert found["hits"] == []


def test_not_food_is_whole_term_only(monkeypatch):
    monkeypatch.setattr(AliasStore, "exact_lookup", lambda self, alias_norm, entity_type=None: [])
    monkeypatch.setattr(AliasStore, "fuzzy_lookup", lambda self, term, entity_type=None, min_sim=0.62, limit=3: [])
    monkeypatch.setattr("kg.lookup.graph_by_name", lambda name: [])
    assert match("热量")["verdict"] == "not_food"
    assert match("章鱼营养价值")["verdict"] == "not_found"


def test_alias_writes_and_stops_fetch(client):
    headers = _admin(client)
    food_key = "zt" + _token()
    canonical = "ING_" + food_key
    request_id = _seed(food_key)
    try:
        response = client.post(
            f"/admin/kg/requests/{request_id}/alias",
            json={"canonical_id": canonical, "alias": food_key},
            headers=headers,
        )
        assert response.status_code == 200, response.text
        with psycopg.connect(config.PG_DSN) as conn:
            alias = conn.execute(
                "SELECT canonical_id FROM dict_alias "
                "WHERE entity_type = 'Ingredient' AND alias_norm = %s",
                (food_key,),
            ).fetchone()
            status = conn.execute(
                "SELECT status FROM kg_ingredient_request WHERE id = %s",
                (request_id,),
            ).fetchone()[0]
            task = conn.execute(
                "SELECT status FROM kg_fetch_task WHERE request_id = %s",
                (request_id,),
            ).fetchone()[0]
            action = conn.execute(
                "SELECT action FROM kg_request_action WHERE request_id = %s",
                (request_id,),
            ).fetchone()[0]
        assert alias[0] == canonical
        assert status == "approved"
        assert task == "rejected"
        assert action == "alias"
    finally:
        _cleanup(food_key, food_key)


def test_occupied_alias_rolls_back(client):
    headers = _admin(client)
    food_key = "zt" + _token()
    request_id = _seed(food_key)
    with psycopg.connect(config.PG_DSN) as conn:
        conn.execute(
            "INSERT INTO dict_alias (entity_type, canonical_id, alias, alias_norm, source, confidence) "
            "VALUES ('Ingredient', 'ING_OWNER', %s, %s, 'seed', 1)",
            (food_key, food_key),
        )
        conn.execute(
            "INSERT INTO dict_alias_candidate "
            "(entity_type, alias, alias_norm, suggested_canonical_id, status) "
            "VALUES ('Ingredient', %s, %s, 'ING_OTHER', 'pending')",
            (food_key, food_key),
        )
    try:
        response = client.post(
            f"/admin/kg/requests/{request_id}/alias",
            json={"canonical_id": "ING_OTHER", "alias": food_key},
            headers=headers,
        )
        assert response.status_code == 409, response.text
        assert response.json()["detail"]["code"] == "alias_occupied"
        with psycopg.connect(config.PG_DSN) as conn:
            status = conn.execute(
                "SELECT status FROM kg_ingredient_request WHERE id = %s",
                (request_id,),
            ).fetchone()[0]
            candidate = conn.execute(
                "SELECT status FROM dict_alias_candidate "
                "WHERE entity_type = 'Ingredient' AND alias_norm = %s",
                (food_key,),
            ).fetchone()[0]
            owners = conn.execute(
                "SELECT canonical_id FROM dict_alias "
                "WHERE entity_type = 'Ingredient' AND alias_norm = %s",
                (food_key,),
            ).fetchall()
        assert status == "queued"
        assert candidate == "pending"
        assert [row[0] for row in owners] == ["ING_OWNER"]
    finally:
        _cleanup(food_key, food_key)


def test_bundle_rejects_impossible_protein(client):
    headers = _admin(client)
    food_key = "zt" + _token()
    request_id = _seed(food_key)
    try:
        response = client.post(
            f"/admin/kg/requests/{request_id}/bundle",
            json=_bundle_body(protein_g="999", name="不该用的名字"),
            headers=headers,
        )
        assert response.status_code == 422, response.text
        assert "超出合理上限" in response.json()["detail"]["message"]
        with psycopg.connect(config.PG_DSN) as conn:
            status = conn.execute(
                "SELECT status FROM kg_ingredient_request WHERE id = %s",
                (request_id,),
            ).fetchone()[0]
            count = conn.execute(
                "SELECT count(*) FROM kg_food_bundle WHERE food_key = %s",
                (food_key,),
            ).fetchone()[0]
        assert status == "queued"
        assert count == 0
    finally:
        _cleanup(food_key)


def test_bundle_links_request_and_second_submit_updates(client):
    headers = _admin(client)
    food_key = "zt" + _token()
    request_id = _seed(food_key)
    try:
        first = client.post(
            f"/admin/kg/requests/{request_id}/bundle",
            json=_bundle_body(name="不该用的名字"),
            headers=headers,
        )
        assert first.status_code == 200, first.text
        second = client.post(
            f"/admin/kg/requests/{request_id}/bundle",
            json=_bundle_body(energy_kcal="50"),
            headers=headers,
        )
        assert second.status_code == 200, second.text
        assert second.json()["action"] == "updated"
        with psycopg.connect(config.PG_DSN) as conn:
            rows = conn.execute(
                "SELECT request_id, canonical_name, status, source_key FROM kg_food_bundle "
                "WHERE food_key = %s AND source_key = 'manual_entry'",
                (food_key,),
            ).fetchall()
            req = conn.execute(
                "SELECT status FROM kg_ingredient_request WHERE id = %s",
                (request_id,),
            ).fetchone()[0]
            task = conn.execute(
                "SELECT status FROM kg_fetch_task WHERE request_id = %s",
                (request_id,),
            ).fetchone()[0]
            action = conn.execute(
                "SELECT action FROM kg_request_action WHERE request_id = %s ORDER BY id",
                (request_id,),
            ).fetchone()[0]
        assert len(rows) == 1
        assert rows[0][0] == request_id
        assert rows[0][1] == food_key
        assert rows[0][2] == "pending_review"
        assert req == "pending_review"
        assert task == "rejected"
        assert action == "bundle"
    finally:
        _cleanup(food_key)


def test_open_usda_bundle_blocks_manual(client):
    headers = _admin(client)
    food_key = "zt" + _token()
    request_id = _seed(food_key)
    with psycopg.connect(config.PG_DSN) as conn:
        conn.execute(
            "INSERT INTO kg_food_bundle (request_id, food_key, canonical_name, source_key, status) "
            "VALUES (%s, %s, %s, 'usda_fdc', 'pending_review')",
            (request_id, food_key, food_key),
        )
    try:
        response = client.post(
            f"/admin/kg/requests/{request_id}/bundle",
            json=_bundle_body(),
            headers=headers,
        )
        assert response.status_code == 409, response.text
        assert response.json()["detail"]["code"] == "usda_bundle_open"
        with psycopg.connect(config.PG_DSN) as conn:
            sources = conn.execute(
                "SELECT source_key FROM kg_food_bundle WHERE food_key = %s",
                (food_key,),
            ).fetchall()
        assert [row[0] for row in sources] == ["usda_fdc"]
    finally:
        _cleanup(food_key)


def test_batch_ignore_noise(client):
    headers = _admin(client)
    food_key = "zt" + _token()
    request_id = _seed(food_key)
    try:
        response = client.post(
            "/admin/kg/requests/batch",
            json={"ids": [request_id], "action": "ignore_noise"},
            headers=headers,
        )
        assert response.status_code == 200, response.text
        with psycopg.connect(config.PG_DSN) as conn:
            status, reason = conn.execute(
                "SELECT status, reject_reason FROM kg_ingredient_request WHERE id = %s",
                (request_id,),
            ).fetchone()
            task = conn.execute(
                "SELECT status FROM kg_fetch_task WHERE request_id = %s",
                (request_id,),
            ).fetchone()[0]
            action = conn.execute(
                "SELECT action FROM kg_request_action WHERE request_id = %s",
                (request_id,),
            ).fetchone()[0]
        assert status == "rejected"
        assert reason == "noise"
        assert task == "rejected"
        assert action == "ignore_noise"
    finally:
        _cleanup(food_key)


def test_hits_sort(client):
    headers = _admin(client)
    prefix = "zs" + _token(4)
    low = prefix + "a"
    high = prefix + "b"
    _seed(low, hit_count=1)
    _seed(high, hit_count=9)
    try:
        response = client.get(
            "/admin/kg/requests",
            params={"sort": "hits", "keyword": prefix, "limit": 50},
            headers=headers,
        )
        assert response.status_code == 200, response.text
        keys = [item["food_key"] for item in response.json()["items"] if item["food_key"] in (low, high)]
        assert keys.index(high) < keys.index(low)
    finally:
        _cleanup(low)
        _cleanup(high)


def test_new_routes_require_login(client):
    assert client.get("/admin/kg/requests/1/matches").status_code == 401
    assert client.post(
        "/admin/kg/requests/1/alias",
        json={"canonical_id": "ING_X", "alias": "测"},
    ).status_code == 401
    assert client.post(
        "/admin/kg/requests/batch",
        json={"ids": [1], "action": "reject"},
    ).status_code == 401
    assert client.post("/admin/kg/requests/1/bundle", json={}).status_code == 401
    assert client.post("/admin/kg/requests/1/fetch").status_code == 401
    assert client.post("/admin/kg/alias-candidates/1/approve").status_code == 401


def test_fetch_button_requeues_without_running_task(client, monkeypatch):
    headers = _admin(client)
    food_key = "zt" + _token()
    request_id = _seed(food_key)
    calls = []

    def fake_popen(cmd, **kwargs):
        calls.append(list(cmd))
        return object()

    monkeypatch.setattr("api.admin.subprocess.Popen", fake_popen)
    with psycopg.connect(config.PG_DSN) as conn:
        conn.execute(
            "UPDATE kg_fetch_task SET status = 'failed', attempts = 3 WHERE request_id = %s",
            (request_id,),
        )
    try:
        response = client.post(f"/admin/kg/requests/{request_id}/fetch", headers=headers)
        assert response.status_code == 200, response.text
        assert calls and calls[0][-1] == str(response.json()["task_id"])
        assert "process_task" not in " ".join(calls[0])
        with psycopg.connect(config.PG_DSN) as conn:
            status, attempts = conn.execute(
                "SELECT status, attempts FROM kg_fetch_task WHERE request_id = %s",
                (request_id,),
            ).fetchone()
        assert status == "queued"
        assert attempts == 0
    finally:
        _cleanup(food_key)


def test_fetch_refuses_after_alias(client, monkeypatch):
    headers = _admin(client)
    food_key = "zt" + _token()
    request_id = _seed(food_key)
    monkeypatch.setattr(
        "api.admin.subprocess.Popen",
        lambda *a, **k: (_ for _ in ()).throw(AssertionError("不该启动")),
    )
    with psycopg.connect(config.PG_DSN) as conn:
        conn.execute(
            "UPDATE kg_ingredient_request SET status = 'approved' WHERE id = %s",
            (request_id,),
        )
    try:
        response = client.post(f"/admin/kg/requests/{request_id}/fetch", headers=headers)
        assert response.status_code == 409, response.text
    finally:
        _cleanup(food_key)


def test_draft_error_rejects_request(client):
    food_key = "zt" + _token()
    request_id = _seed(food_key)
    with psycopg.connect(config.PG_DSN) as conn:
        task_id = conn.execute(
            "SELECT id FROM kg_fetch_task WHERE request_id = %s",
            (request_id,),
        ).fetchone()[0]

    def boom(key):
        raise DraftError("画像不可用")

    import kg.fetch as fetch_mod
    original = fetch_mod.draft_mod.get_draft
    fetch_mod.draft_mod.get_draft = boom
    try:
        result = process_task({
            "id": task_id, "request_id": request_id, "food_key": food_key, "attempts": 1,
        })
        assert result["status"] == "failed"
        with psycopg.connect(config.PG_DSN) as conn:
            status = conn.execute(
                "SELECT status FROM kg_ingredient_request WHERE id = %s",
                (request_id,),
            ).fetchone()[0]
        assert status == "rejected"
    finally:
        fetch_mod.draft_mod.get_draft = original
        _cleanup(food_key)


def test_first_no_match_keeps_request_queued(client):
    food_key = "zt" + _token()
    request_id = _seed(food_key)
    with psycopg.connect(config.PG_DSN) as conn:
        task_id = conn.execute(
            "SELECT id FROM kg_fetch_task WHERE request_id = %s",
            (request_id,),
        ).fetchone()[0]

    draft = FoodDraft(
        canonical_name=food_key,
        english_name="test",
        fdc_query="test",
        identity_keywords=["zzzznotfound"],
    )

    class Empty:
        def search(self, query):
            return []

        def get_food(self, fdc_id):
            raise AssertionError("没有候选，不该拉详情")

    import kg.fetch as fetch_mod
    original = fetch_mod.draft_mod.get_draft
    fetch_mod.draft_mod.get_draft = lambda key: draft
    try:
        result = process_task(
            {"id": task_id, "request_id": request_id, "food_key": food_key, "attempts": 1},
            client=Empty(),
        )
        assert result["status"] == "no_match"
        with psycopg.connect(config.PG_DSN) as conn:
            status = conn.execute(
                "SELECT status FROM kg_ingredient_request WHERE id = %s",
                (request_id,),
            ).fetchone()[0]
            task = conn.execute(
                "SELECT status FROM kg_fetch_task WHERE id = %s",
                (task_id,),
            ).fetchone()[0]
        assert status == "queued"
        assert task == "queued"
    finally:
        fetch_mod.draft_mod.get_draft = original
        _cleanup(food_key)


def test_approve_alias_candidate(client):
    headers = _admin(client)
    alias = "zt" + _token()
    canonical = "ING_" + alias
    with psycopg.connect(config.PG_DSN) as conn:
        candidate_id = conn.execute(
            "INSERT INTO dict_alias_candidate "
            "(entity_type, alias, alias_norm, suggested_canonical_id, status) "
            "VALUES ('Ingredient', %s, %s, %s, 'pending') RETURNING id",
            (alias, alias, canonical),
        ).fetchone()[0]
    try:
        response = client.post(
            f"/admin/kg/alias-candidates/{candidate_id}/approve",
            headers=headers,
        )
        assert response.status_code == 200, response.text
        with psycopg.connect(config.PG_DSN) as conn:
            linked = conn.execute(
                "SELECT canonical_id FROM dict_alias "
                "WHERE entity_type = 'Ingredient' AND alias_norm = %s",
                (alias,),
            ).fetchone()
            status = conn.execute(
                "SELECT status FROM dict_alias_candidate WHERE id = %s",
                (candidate_id,),
            ).fetchone()[0]
        assert linked[0] == canonical
        assert status == "approved"
    finally:
        _cleanup("unused", alias)
