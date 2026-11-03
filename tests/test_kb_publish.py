"""添加不写向量。通过失败仍是待审。编辑和驳回按文档删向量。"""
from __future__ import annotations

from pathlib import Path

import pytest

from kb.common import KbError
from tests.helpers import bearer, kb_staff, requires_pg

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
    return kb_staff(client)


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


def test_foreign_tenant_rejected(client, quiet_index):
    tokens = _admin(client)
    headers = bearer(tokens["access_token"])
    denied = client.post(
        "/admin/kb/docs",
        headers=headers,
        data={"tenant_id": "otherco", "title": "报销制度", "doc_kind": "policy"},
        files={"file": ("rule.md", "# 制度\n正文\n".encode(), "text/markdown")},
    )
    assert denied.status_code == 403
    assert denied.json()["detail"]["code"] == "forbidden"

    row = _add(client, headers)
    edited = client.put(
        f"/admin/kb/docs/{row['id']}",
        headers=headers,
        data={"tenant_id": "otherco", "title": "报销制度修订", "doc_kind": "tech"},
    )
    assert edited.status_code == 403
    assert edited.json()["detail"]["code"] == "forbidden"
    listed = client.get("/admin/kb/docs?title=报销制度", headers=headers).json()["items"]
    found = next(item for item in listed if item["id"] == row["id"])
    assert found["tenant_id"] == "acme"
    assert found["title"] == "报销制度"


def test_tenant_list_defaults_to_member(client):
    tokens = _admin(client)
    listed = client.get("/kb/tenants", headers=bearer(tokens["access_token"]))
    assert listed.status_code == 200, listed.text
    names = listed.json()["tenants"]
    assert names == sorted(names)
    assert names[0] == "acme"
    assert "default_renter" in names
    assert "otherco" not in names


def test_file_too_large(client, quiet_index, monkeypatch):
    monkeypatch.setattr("kb.publish.UPLOAD_MAX", 4)
    tokens = _admin(client)
    response = client.post(
        "/admin/kb/docs",
        headers=bearer(tokens["access_token"]),
        data={"tenant_id": "acme", "title": "报销制度", "doc_kind": "policy"},
        files={"file": ("rule.md", b"0123456789", "text/markdown")},
    )
    assert response.status_code == 400
    assert response.json()["detail"]["code"] == "file_too_large"


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


def test_source_file_returns_original(client, quiet_index):
    tokens = _admin(client)
    headers = bearer(tokens["access_token"])
    row = _add(client, headers)
    got = client.get(f"/admin/kb/docs/{row['id']}/file", headers=headers)
    assert got.status_code == 200, got.text
    assert "差旅费按票据实报" in got.text
    assert got.headers["content-type"].startswith("text/markdown")
    assert "inline" in got.headers["content-disposition"].lower()

    missing = client.get(f"/admin/kb/docs/{row['id']}/file")
    assert missing.status_code == 401


def test_source_pdf_and_docx(client, quiet_index):
    tokens = _admin(client)
    headers = bearer(tokens["access_token"])
    pdf = client.post(
        "/admin/kb/docs",
        headers=headers,
        data={"tenant_id": "acme", "title": "流程图", "doc_kind": "tech"},
        files={"file": ("流程图.pdf", b"%PDF-1.4 fake", "application/pdf")},
    )
    assert pdf.status_code == 200, pdf.text
    pdf_id = pdf.json()["id"]
    raw = client.get(f"/admin/kb/docs/{pdf_id}/file", headers=headers)
    assert raw.status_code == 200, raw.text
    assert raw.headers["content-type"].startswith("application/pdf")
    assert raw.content.startswith(b"%PDF")

    from io import BytesIO
    from docx import Document

    buf = BytesIO()
    document = Document()
    document.add_paragraph("报销按票据。")
    table = document.add_table(rows=1, cols=2)
    table.rows[0].cells[0].text = "项目"
    table.rows[0].cells[1].text = "差旅"
    document.save(buf)
    word = client.post(
        "/admin/kb/docs",
        headers=headers,
        data={"tenant_id": "acme", "title": "报销说明", "doc_kind": "policy"},
        files={"file": (
            "说明.docx",
            buf.getvalue(),
            "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
        )},
    )
    assert word.status_code == 200, word.text
    text = client.get(f"/admin/kb/docs/{word.json()['id']}/file?text=1", headers=headers)
    assert text.status_code == 200, text.text
    assert "报销按票据" in text.text
    assert "项目" in text.text and "差旅" in text.text


def test_source_missing_file(client, quiet_index):
    tokens = _admin(client)
    headers = bearer(tokens["access_token"])
    row = _add(client, headers)
    from kb.store import get_document

    path = Path(get_document(row["id"])["storage_path"])
    path.unlink()
    got = client.get(f"/admin/kb/docs/{row['id']}/file", headers=headers)
    assert got.status_code == 404
    assert got.json()["detail"]["code"] == "not_found"
