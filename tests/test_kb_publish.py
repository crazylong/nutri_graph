"""添加不写向量。通过失败仍是待审。编辑和驳回按文档删向量。"""
from __future__ import annotations

from pathlib import Path

import pytest

from kb.common import KbError
from tests.helpers import bearer, login, requires_pg

pytestmark = requires_pg


@pytest.fixture
def quiet_index(monkeypatch, tmp_path: Path):
    import config

    monkeypatch.setattr(config, "KB_DATA_DIR", str(tmp_path))
    calls = {"delete": [], "insert": 0}

    def delete_doc(doc_id: str) -> None:
        calls["delete"].append(doc_id)

    def insert_chunks(rows) -> None:
        calls["insert"] += 1

    monkeypatch.setattr("kb.publish.milvus.delete_doc", delete_doc)
    monkeypatch.setattr("kb.publish.milvus.insert_chunks", insert_chunks)
    monkeypatch.setattr("kb.publish.embed_mm.embed_text", lambda text: [0.1, 0.2])
    monkeypatch.setattr("kb.publish.embed_mm.embed_image_bytes", lambda raw, mime="image/png": [0.2, 0.1])
    monkeypatch.setattr("kb.publish.caption.caption_bytes", lambda *args, **kwargs: "图:说明")
    return calls


def _admin(client):
    tokens = login(client)
    import config
    import psycopg

    with psycopg.connect(config.PG_DSN) as conn:
        conn.execute(
            "UPDATE app_user SET role = 'admin' WHERE user_id = %s",
            (tokens["user"]["user_id"],),
        )
    return tokens


def _add(client, headers, tenant="acme"):
    response = client.post(
        "/admin/kb/docs",
        headers=headers,
        data={"tenant_id": tenant, "title": "报销制度", "doc_kind": "policy"},
        files={"file": ("rule.md", "# 制度\n## 报销\n差旅费按票据实报。\n".encode(), "text/markdown")},
    )
    assert response.status_code == 200, response.text
    return response.json()


def test_add_writes_no_vectors(client, quiet_index):
    tokens = _admin(client)
    row = _add(client, bearer(tokens["access_token"]))
    assert row["status"] == "pending"
    assert quiet_index["insert"] == 0
    assert quiet_index["delete"] == []


def test_bad_tenant_rejected(client, quiet_index):
    tokens = _admin(client)
    response = client.post(
        "/admin/kb/docs",
        headers=bearer(tokens["access_token"]),
        data={"tenant_id": "bad tenant", "title": "报销制度", "doc_kind": "policy"},
        files={"file": ("rule.md", "# 制度\n正文\n".encode(), "text/markdown")},
    )
    assert response.status_code == 400
    assert response.json()["detail"]["code"] == "bad_tenant"


def test_approve_failure_stays_pending(client, quiet_index, monkeypatch):
    tokens = _admin(client)
    headers = bearer(tokens["access_token"])
    row = _add(client, headers)

    def boom(text):
        raise KbError("嵌入失败", status=502, code="embed_failed")

    monkeypatch.setattr("kb.publish.embed_mm.embed_text", boom)
    response = client.post(f"/admin/kb/docs/{row['id']}/approve", headers=headers)
    assert response.status_code == 502
    listed = client.get("/admin/kb/docs?status=pending", headers=headers).json()["items"]
    found = next(item for item in listed if item["id"] == row["id"])
    assert found["status"] == "pending"
    assert quiet_index["insert"] == 0


def test_edit_and_reject_delete_vectors(client, quiet_index, monkeypatch):
    tokens = _admin(client)
    headers = bearer(tokens["access_token"])
    row = _add(client, headers)
    monkeypatch.setattr("kb.publish.embed_mm.embed_text", lambda text: [0.1, 0.2])
    approved = client.post(f"/admin/kb/docs/{row['id']}/approve", headers=headers)
    assert approved.status_code == 200, approved.text
    assert approved.json()["status"] == "approved"
    assert quiet_index["insert"] == 1

    edited = client.put(
        f"/admin/kb/docs/{row['id']}",
        headers=headers,
        data={"tenant_id": "acme", "title": "报销制度修订", "doc_kind": "tech"},
    )
    assert edited.status_code == 200, edited.text
    assert edited.json()["status"] == "pending"
    assert row["id"] in quiet_index["delete"]

    rejected = client.post(
        f"/admin/kb/docs/{row['id']}/reject",
        headers=headers,
        data={"reason": "标题不对"},
    )
    assert rejected.status_code == 200, rejected.text
    assert rejected.json()["status"] == "rejected"
    assert quiet_index["delete"].count(row["id"]) >= 2
