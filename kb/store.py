"""文档主数据。向量不在这里。"""
from __future__ import annotations

import shutil
import uuid
from pathlib import Path
from typing import Any

from psycopg.rows import dict_row
from psycopg.types.json import Jsonb

import db
from kb.common import KbError, data_root, doc_dir


def insert_document(row: dict) -> None:
    with db.connect() as conn:
        conn.execute(
            """
            INSERT INTO kb_document (
                id, tenant_id, title, doc_kind, status, filename, storage_path,
                uploader_user_id, uploader_phone_mask, error
            ) VALUES (%s,%s,%s,%s,'pending',%s,%s,%s,%s,'')
            """,
            (
                row["id"], row["tenant_id"], row["title"], row["doc_kind"],
                row["filename"], row["storage_path"],
                row["uploader_user_id"], row["uploader_phone_mask"],
            ),
        )


def get_document(doc_id: str) -> dict | None:
    with db.connect() as conn, conn.cursor(row_factory=dict_row) as cur:
        cur.execute("SELECT * FROM kb_document WHERE id = %s", (doc_id,))
        return cur.fetchone()


def list_documents(filters: dict) -> list[dict]:
    where = ["TRUE"]
    args: list[Any] = []
    for column, key in (
        ("tenant_id", "tenant_id"),
        ("doc_kind", "doc_kind"),
        ("uploader_phone_mask", "uploader_phone_mask"),
        ("status", "status"),
    ):
        value = (filters.get(key) or "").strip()
        if value:
            where.append(f"{column} = %s")
            args.append(value)
    title = (filters.get("title") or "").strip()
    if title:
        where.append("title ILIKE %s")
        args.append(f"%{title}%")
    sql = (
        "SELECT id::text, tenant_id, title, doc_kind, status, filename, "
        "uploader_phone_mask, error, created_at, updated_at "
        "FROM kb_document WHERE " + " AND ".join(where) +
        " ORDER BY updated_at DESC LIMIT 200"
    )
    with db.connect() as conn, conn.cursor(row_factory=dict_row) as cur:
        cur.execute(sql, args)
        return list(cur.fetchall())


def list_tenants() -> list[str]:
    with db.connect() as conn:
        rows = conn.execute("SELECT name FROM tenant ORDER BY 1").fetchall()
    return [row[0] for row in rows]


def mark(doc_id: str, status: str, error: str) -> None:
    with db.connect() as conn:
        conn.execute(
            """
            UPDATE kb_document
            SET status = %s, error = %s, updated_at = now()
            WHERE id = %s
            """,
            (status, error, doc_id),
        )


def update_meta(doc_id: str, *, tenant_id: str, title: str, doc_kind: str, filename: str, storage_path: str) -> None:
    with db.connect() as conn:
        conn.execute(
            """
            UPDATE kb_document
            SET tenant_id = %s, title = %s, doc_kind = %s, filename = %s,
                storage_path = %s, status = 'pending', error = '', updated_at = now()
            WHERE id = %s
            """,
            (tenant_id, title, doc_kind, filename, storage_path, doc_id),
        )


def replace_chunks(doc_id: str, rows: list[dict]) -> None:
    with db.connect() as conn:
        conn.execute("DELETE FROM kb_chunk WHERE doc_id = %s", (doc_id,))
        for row in rows:
            conn.execute(
                """
                INSERT INTO kb_chunk (
                    id, doc_id, tenant_id, block_kind, text, page_no, bbox,
                    heading_path, image_path, image_md5
                ) VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
                """,
                (
                    row["chunk_id"], doc_id, row["tenant_id"], row["block_kind"],
                    row["text"], row.get("page_no"),
                    Jsonb(row["bbox"]) if row.get("bbox") else None,
                    row.get("heading_path") or "",
                    row.get("image_path") or None,
                    row.get("image_md5"),
                ),
            )


def delete_chunks(doc_id: str) -> None:
    with db.connect() as conn:
        conn.execute("DELETE FROM kb_chunk WHERE doc_id = %s", (doc_id,))


def delete_document(doc_id: str) -> None:
    with db.connect() as conn:
        conn.execute("DELETE FROM kb_document WHERE id = %s", (doc_id,))


def get_chunk(chunk_id: str) -> dict | None:
    with db.connect() as conn, conn.cursor(row_factory=dict_row) as cur:
        cur.execute(
            """
            SELECT c.id, c.tenant_id, c.block_kind, c.text, c.heading_path, c.image_path,
                   d.status
            FROM kb_chunk c
            JOIN kb_document d ON d.id = c.doc_id
            WHERE c.id = %s
            """,
            (chunk_id,),
        )
        return cur.fetchone()


def remove_files(doc_id: str) -> None:
    folder = doc_dir(doc_id)
    if folder.exists():
        shutil.rmtree(folder, ignore_errors=True)


def new_id() -> str:
    return str(uuid.uuid4())


def source_path(doc_id: str, filename: str) -> Path:
    folder = doc_dir(doc_id)
    return folder / ("source" + Path(filename).suffix.lower())


def source_file(doc_id: str) -> tuple[dict, Path]:
    """已入库原文。路径必须落在知识库数据目录里。"""
    doc = get_document(doc_id)
    if doc is None:
        raise KbError("文档不存在", status=404, code="not_found")
    path = Path(doc["storage_path"]).resolve()
    root = data_root().resolve()
    if not path.is_file() or (path != root and root not in path.parents):
        raise KbError("原文不存在", status=404, code="not_found")
    return doc, path
