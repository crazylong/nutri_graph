"""添加、编辑、通过、驳回、删除。接口返回时，检索结果已经是新状态。"""
from __future__ import annotations

import logging
import shutil
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

log = logging.getLogger("kb.publish")

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


def reindex(doc_id: str) -> dict:
    """整篇重嵌。解析失败时旧向量、旧块、旧图都留着，状态仍是已通过。

    新图先写到 images.new，记录里的路径仍是 images/。删掉旧数据之后再改名，
    改名之后才检查文件在不在。检查放在改名之前会把每一篇都判失败。
    """
    doc = _need(doc_id)
    path = Path(doc["storage_path"])
    staging = path.parent / "images.new"
    live = path.parent / "images"
    if staging.exists():
        shutil.rmtree(staging)
    warnings: list[str] = []
    try:
        prepared = _index(doc, warnings, write_dir=staging, record_dir=live)
    except Exception as exc:
        if staging.exists():
            shutil.rmtree(staging)
        _mark_reindex_error(doc_id, doc["status"], exc)
        if isinstance(exc, KbError):
            raise
        raise KbError("重嵌失败", status=502, code="reindex_failed") from exc
    try:
        milvus.delete_doc(doc_id)
        delete_chunks(doc_id)
        if live.exists():
            shutil.rmtree(live)
        if staging.exists():
            staging.replace(live)
        missing = [
            row["image_path"]
            for row in prepared
            if row.get("image_path") and not Path(row["image_path"]).is_file()
        ]
        if missing:
            raise KbError("重嵌后图片文件缺失", code="image_missing")
        milvus.insert_chunks(prepared)
        replace_chunks(doc_id, prepared)
    except Exception as exc:
        milvus.delete_doc(doc_id)
        delete_chunks(doc_id)
        _mark_reindex_error(doc_id, "pending", exc)
        if isinstance(exc, KbError):
            raise
        raise KbError("重嵌失败", status=502, code="reindex_failed") from exc
    images = sum(1 for row in prepared if row["block_kind"] == "image")
    log.info("重嵌 %s 图片 %d 张", doc_id, images)
    mark(doc_id, "approved", "；".join(warnings))
    fresh = dict(get_document(doc_id) or {})
    fresh["image_count"] = images
    return fresh


def _mark_reindex_error(doc_id: str, status: str, exc: Exception) -> None:
    message = exc.message if isinstance(exc, KbError) else str(exc)
    mark(doc_id, status, f"重嵌失败：{message}"[:500])


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


def _index(
    doc: dict,
    warnings: list[str],
    *,
    write_dir: Path | None = None,
    record_dir: Path | None = None,
) -> list[dict]:
    """一张图两行：图像向量一行，描述的文本向量一行。路径都记最终目录。

    write_dir 是字节落地的目录。重嵌时它是 images.new，record_dir 仍是 images，
    这样改名之后库里的路径不用再改。审核通过时两个目录相同。
    """
    path = Path(doc["storage_path"])
    record_folder = record_dir or (path.parent / "images")
    write_folder = write_dir or record_folder
    blocks = chunk.prepare(parse.parse_file(path, doc["filename"]), doc["doc_kind"])
    if not blocks:
        raise KbError("没有解析出可入库的内容", code="empty_doc")
    write_folder.mkdir(parents=True, exist_ok=True)
    rows = []
    for block in blocks:
        if block["block_kind"] == "image":
            rows.extend(_image_rows(doc, block, warnings, write_folder, record_folder))
            continue
        rows.append(_text_row(doc, block))
    return rows


def _text_row(doc: dict, block: dict) -> dict:
    text = block["text"]
    return _row(
        doc, block, chunk_id=uuid.uuid4().hex, text=text, image_path="",
        dense=embed_mm.embed_text(text),
    )


def _image_rows(doc: dict, block: dict, warnings: list[str], write_folder: Path, record_folder: Path) -> list[dict]:
    raw = block["image_bytes"]
    image_id = uuid.uuid4().hex
    if len(raw) > IMAGE_MAX:
        raise KbError(f"{block.get('image_name') or '图片'} 超过 10MB", code="image_too_large")
    if len(raw) > IMAGE_WARN:
        warnings.append(f"{block.get('image_name') or image_id} 大于 5MB")
    ext = block.get("image_ext") or ".png"
    mime = _MIME.get(ext, "image/png")
    text = caption.caption_bytes(
        raw,
        mime=mime,
        heading=block.get("heading_path") or "",
        filename=block.get("image_name") or image_id,
        prev_text=block.get("prev_text") or "",
        next_text=block.get("next_text") or "",
    )
    (write_folder / f"{image_id}{ext}").write_bytes(raw)
    image_path = str(record_folder / f"{image_id}{ext}")
    shared = {
        "text": text,
        "image_path": image_path,
        "image_md5": block.get("image_md5"),
    }
    image_row = _row(
        doc, block, chunk_id=image_id, dense=embed_mm.embed_image_bytes(raw, mime=mime), **shared,
    )
    summary = _row(
        doc, block, chunk_id=uuid.uuid4().hex, dense=embed_mm.embed_text(text), **shared,
    )
    summary["block_kind"] = "text"
    return [image_row, summary]


def _row(doc: dict, block: dict, *, chunk_id: str, text: str, dense: list[float], image_path: str = "", image_md5: str | None = None) -> dict:
    return {
        "chunk_id": chunk_id,
        "doc_id": str(doc["id"]),
        "tenant_id": doc["tenant_id"],
        "block_kind": block["block_kind"],
        "text": text,
        "page_no": block.get("page_no"),
        "bbox": block.get("bbox"),
        "heading_path": block.get("heading_path") or "",
        "image_path": image_path,
        "image_md5": image_md5 if image_md5 is not None else block.get("image_md5"),
        "dense": dense,
    }


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
