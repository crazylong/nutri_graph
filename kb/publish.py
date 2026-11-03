"""添加、编辑、通过、驳回、删除。接口返回时，检索结果已经是新状态。"""
from __future__ import annotations

import uuid
from pathlib import Path

from kb import caption, chunk, embed_mm, memory, milvus, parse
from kb.common import IMAGE_MAX, IMAGE_WARN, UPLOAD_MAX, KbError, check_kind, check_tenant
from kb.store import (
    delete_chunks,
    delete_document,
    get_document,
    insert_document,
    mark,
    new_id,
    remove_files,
    replace_chunks,
    source_path,
    update_meta,
)

_MIME = {
    ".png": "image/png",
    ".jpg": "image/jpeg",
    ".jpeg": "image/jpeg",
    ".webp": "image/webp",
    ".gif": "image/gif",
    ".bmp": "image/bmp",
}


def add_document(*, tenant_id: str, title: str, doc_kind: str, filename: str, raw: bytes, user: dict) -> dict:
    tenant = check_tenant(tenant_id)
    _ensure_member(user, tenant)
    kind = check_kind(doc_kind)
    title = (title or "").strip()
    if not title:
        raise KbError("标题必填", code="bad_title")
    _check_upload(filename, raw)
    doc_id = new_id()
    path = source_path(doc_id, filename)
    path.write_bytes(raw)
    insert_document({
        "id": doc_id,
        "tenant_id": tenant,
        "title": title,
        "doc_kind": kind,
        "filename": Path(filename).name,
        "storage_path": str(path),
        "uploader_user_id": str(user.get("user_id") or ""),
        "uploader_phone_mask": user.get("phone_mask") or "",
    })
    return get_document(doc_id)


def edit_document(doc_id: str, *, tenant_id: str, title: str, doc_kind: str, filename: str | None, raw: bytes | None, user: dict) -> dict:
    doc = _need(doc_id)
    tenant = check_tenant(tenant_id)
    _ensure_member(user, tenant)
    kind = check_kind(doc_kind)
    title = (title or "").strip()
    if not title:
        raise KbError("标题必填", code="bad_title")
    stored_name = doc["filename"]
    stored_path = doc["storage_path"]
    if raw is not None:
        _check_upload(filename or stored_name, raw)
        stored_name = Path(filename or stored_name).name
        path = source_path(doc_id, stored_name)
        path.write_bytes(raw)
        stored_path = str(path)
    milvus.delete_doc(doc_id)
    delete_chunks(doc_id)
    update_meta(
        doc_id,
        tenant_id=tenant,
        title=title,
        doc_kind=kind,
        filename=stored_name,
        storage_path=stored_path,
    )
    return get_document(doc_id)


def approve(doc_id: str) -> dict:
    doc = _need(doc_id)
    if doc["status"] == "approved":
        return doc
    warnings: list[str] = []
    try:
        prepared = _index(doc, warnings)
        milvus.delete_doc(doc_id)
        milvus.insert_chunks(prepared)
        replace_chunks(doc_id, prepared)
    except KbError as exc:
        milvus.delete_doc(doc_id)
        delete_chunks(doc_id)
        mark(doc_id, "pending", exc.message)
        raise
    except Exception as exc:
        milvus.delete_doc(doc_id)
        delete_chunks(doc_id)
        mark(doc_id, "pending", str(exc)[:500])
        raise KbError("入库失败", status=502, code="publish_failed") from exc
    mark(doc_id, "approved", "；".join(warnings))
    return get_document(doc_id)


def reject(doc_id: str, reason: str = "") -> dict:
    _need(doc_id)
    milvus.delete_doc(doc_id)
    delete_chunks(doc_id)
    mark(doc_id, "rejected", (reason or "").strip()[:500])
    return get_document(doc_id)


def remove(doc_id: str) -> None:
    _need(doc_id)
    milvus.delete_doc(doc_id)
    delete_document(doc_id)
    remove_files(doc_id)


def _index(doc: dict, warnings: list[str]) -> list[dict]:
    path = Path(doc["storage_path"])
    blocks = chunk.prepare(parse.parse_file(path, doc["filename"]), doc["doc_kind"])
    if not blocks:
        raise KbError("没有解析出可入库的内容", code="empty_doc")
    folder = path.parent / "images"
    folder.mkdir(parents=True, exist_ok=True)
    rows = []
    for block in blocks:
        chunk_id = uuid.uuid4().hex
        image_path = ""
        digest = block.get("image_md5")
        text = block["text"]
        if block["block_kind"] == "image":
            raw = block["image_bytes"]
            if len(raw) > IMAGE_MAX:
                raise KbError(f"{block.get('image_name') or '图片'} 超过 10MB", code="image_too_large")
            if len(raw) > IMAGE_WARN:
                warnings.append(f"{block.get('image_name') or chunk_id} 大于 5MB")
            ext = block.get("image_ext") or ".png"
            mime = _MIME.get(ext, "image/png")
            text = caption.caption_bytes(
                raw, mime=mime, heading=block.get("heading_path") or "",
                filename=block.get("image_name") or chunk_id,
            )
            dest = folder / f"{chunk_id}{ext}"
            dest.write_bytes(raw)
            image_path = str(dest)
            vector = embed_mm.embed_image_bytes(raw, mime=mime)
        else:
            vector = embed_mm.embed_text(text)
        rows.append({
            "chunk_id": chunk_id,
            "doc_id": str(doc["id"]),
            "tenant_id": doc["tenant_id"],
            "block_kind": block["block_kind"],
            "text": text,
            "page_no": block.get("page_no"),
            "bbox": block.get("bbox"),
            "heading_path": block.get("heading_path") or "",
            "image_path": image_path,
            "image_md5": digest,
            "dense": vector,
        })
    return rows


def _need(doc_id: str) -> dict:
    doc = get_document(doc_id)
    if doc is None:
        raise KbError("文档不存在", status=404, code="not_found")
    return doc


def _ensure_member(user: dict, tenant: str) -> None:
    if not memory.member(str(user.get("user_id") or ""), tenant):
        raise KbError("不能使用这个租户", status=403, code="forbidden")


def _check_upload(filename: str, raw: bytes) -> None:
    suffix = Path(filename or "").suffix.lower()
    if suffix not in {".md", ".markdown", ".pdf", ".docx"}:
        raise KbError("只接收 md、pdf、docx", code="bad_file")
    if not raw:
        raise KbError("文件是空的", code="empty_file")
    if len(raw) > UPLOAD_MAX:
        raise KbError("文件超过 32MB", code="file_too_large")
