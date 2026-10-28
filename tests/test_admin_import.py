"""
tests/test_admin_import.py —— 管理员批量导入通道（/admin/imports/*）

与公开导入（/kg/import，test_import_manual 覆盖）的差异点：
  1. 整组挂 require_admin —— 非管理员 403；
  2. 按管理员计额（reviewer 列），不占 IP 限额；
  3. approve 是「逐条翻候选 + 范围合并」，只碰本批 bundle。
"""
from __future__ import annotations

import io

import pytest

from tests.helpers import bearer, login, requires_pg

pytestmark = requires_pg


def _promote(user_id: str) -> None:
    import psycopg

    import config

    with psycopg.connect(config.PG_DSN) as conn:
        conn.execute("UPDATE app_user SET role = 'admin' WHERE user_id = %s", (user_id,))


def _admin_headers(client) -> dict:
    tokens = login(client)
    _promote(tokens["user"]["user_id"])
    return bearer(tokens["access_token"])


def _csv_blob() -> bytes:
    """一份能过校验的最小 CSV（两行）。"""
    header = (
        "名称,分类,性味,别名,能量(kcal),蛋白质(g),脂肪(g),碳水化合物(g),来源\n"
    )
    row1 = "测试导入一号,谷物,平,一号粥,46,1.4,0.7,8.4,测试来源\n"
    row2 = "测试导入二号,蔬菜,凉,二号菜,20,2.0,0.3,3.0,测试来源\n"
    return (header + row1 + row2).encode("utf-8")


def test_meta_requires_admin(client):
    assert client.get("/admin/imports/meta").status_code == 401
    tokens = login(client)
    r = client.get("/admin/imports/meta", headers=bearer(tokens["access_token"]))
    assert r.status_code == 403


def test_admin_meta_has_template_and_limits(client):
    headers = _admin_headers(client)
    r = client.get("/admin/imports/meta", headers=headers)
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["template"]["xlsx"].startswith("/template/")
    assert body["limits"]["max_rows"] == 500
    assert "columns" in body and body["columns"]


def test_preview_commit_approve_roundtrip(client):
    headers = _admin_headers(client)
    files = {"file": ("test.csv", io.BytesIO(_csv_blob()), "text/csv")}
    r = client.post("/admin/imports/preview", files=files, headers=headers)
    assert r.status_code == 200, r.text
    body = r.json()
    job_id = body["job_id"]
    assert body["summary"]["importable"] >= 2

    # commit → 写 bundle（pending_review）
    r = client.post(f"/admin/imports/{job_id}/commit", headers=headers)
    assert r.status_code == 200, r.text
    staged = r.json()["staged"]
    assert staged >= 2

    # 重复 commit 幂等拒绝
    r = client.post(f"/admin/imports/{job_id}/commit", headers=headers)
    assert r.status_code == 409

    # approve：逐条翻候选 + 范围合并。写图动作需要 Neo4j —— 连不上时这里
    # 会失败，所以只断言「不 500、返回结构正确」，merge 结果允许为 0。
    r = client.post(f"/admin/imports/{job_id}/approve", headers=headers)
    assert r.status_code == 200, r.text
    result = r.json()
    assert "approved" in result
    assert "merge" in result and "nodes" in result["merge"]

    # 历史列表能查到这条 job
    r = client.get("/admin/imports", headers=headers)
    assert r.status_code == 200
    ids = [i["job_id"] for i in r.json()["items"]]
    assert job_id in ids


def test_preview_quota_is_per_admin_not_ip(client):
    """同一管理员能连发多批（不占 IP 额），但单批上限仍受控。"""
    headers = _admin_headers(client)
    files = {"file": ("a.csv", io.BytesIO(_csv_blob()), "text/csv")}
    for _ in range(3):
        r = client.post("/admin/imports/preview", files=files, headers=headers)
        assert r.status_code == 200, r.text


def test_commit_bad_job_id(client):
    headers = _admin_headers(client)
    r = client.post("/admin/imports/not-a-uuid/commit", headers=headers)
    assert r.status_code == 404


def test_approve_nothing_returns_conflict(client):
    headers = _admin_headers(client)
    import uuid

    r = client.post(f"/admin/imports/{uuid.uuid4()}/approve", headers=headers)
    # 没有对应 bundle → 409（nothing_to_approve）
    assert r.status_code == 409
