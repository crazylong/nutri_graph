"""企业知识库的后台：查询、审核。整组要管理员令牌。"""
from __future__ import annotations

import threading
from pathlib import Path

from fastapi import APIRouter, Depends, File, Form, Query, UploadFile
from fastapi.responses import FileResponse, JSONResponse, PlainTextResponse

from api.deps import require_admin
from kb.common import KbError
from kb.publish import add_document, approve, edit_document, reject, remove
from kb.store import list_documents, list_tenants, source_file

import db

router = APIRouter(prefix="/admin/kb", tags=["admin-kb"], dependencies=[Depends(require_admin)])

# 评估运行锁：防止并发重复触发（Ragas 要调模型，重复跑既费钱又打架）
_eval_lock = threading.Lock()
_eval_running = False
_eval_error: str | None = None


def _run_eval_background() -> None:
    """后台线程跑评估，结果写 kb_eval_run 表；异常记到 _eval_error。"""
    global _eval_running, _eval_error
    from eval.kb_run import run_eval

    try:
        run_eval()
    except Exception as exc:  # noqa: BLE001 —— 线程内不能把异常抛到请求方，就地记录
        _eval_error = str(exc)[:500]
    finally:
        _eval_running = False


def _fail(exc: KbError) -> JSONResponse:
    return JSONResponse(
        status_code=exc.status,
        content={"detail": {"code": exc.code, "message": exc.message}},
    )


def _row(row: dict | None) -> dict | None:
    if row is None:
        return None
    out = dict(row)
    out["id"] = str(out["id"])
    for key in ("created_at", "updated_at"):
        value = out.get(key)
        if value is not None and hasattr(value, "isoformat"):
            out[key] = value.isoformat()
    return out


@router.get("/docs")
def docs(
    tenant_id: str = "",
    title: str = "",
    doc_kind: str = "",
    uploader_phone_mask: str = "",
    status: str = "",
):
    try:
        rows = list_documents({
            "tenant_id": tenant_id,
            "title": title,
            "doc_kind": doc_kind,
            "uploader_phone_mask": uploader_phone_mask,
            "status": status,
        })
    except Exception as exc:
        return _missing(exc)
    return {"items": [_row(row) for row in rows]}


@router.get("/tenants")
def tenants():
    try:
        return {"tenants": list_tenants()}
    except Exception as exc:
        return _missing(exc)


@router.post("/docs")
async def create(
    user: dict = Depends(require_admin),
    tenant_id: str = Form(...),
    title: str = Form(...),
    doc_kind: str = Form(...),
    file: UploadFile = File(...),
):
    try:
        row = add_document(
            tenant_id=tenant_id,
            title=title,
            doc_kind=doc_kind,
            filename=file.filename or "",
            raw=await file.read(),
            user=user,
        )
    except KbError as exc:
        return _fail(exc)
    return _row(row)


@router.put("/docs/{doc_id}")
async def update(
    doc_id: str,
    user: dict = Depends(require_admin),
    tenant_id: str = Form(...),
    title: str = Form(...),
    doc_kind: str = Form(...),
    file: UploadFile | None = File(None),
):
    raw = await file.read() if file is not None else None
    name = file.filename if file is not None else None
    try:
        row = edit_document(
            doc_id,
            tenant_id=tenant_id,
            title=title,
            doc_kind=doc_kind,
            filename=name,
            raw=raw or None,
            user=user,
        )
    except KbError as exc:
        return _fail(exc)
    return _row(row)


_FILE_TYPES = {
    ".pdf": "application/pdf",
    ".md": "text/markdown",
    ".markdown": "text/markdown",
    ".docx": "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
}


def _docx_text(path: Path) -> str:
    """按文档顺序抽出段落和表格，供页面直接阅读。"""
    from docx import Document
    from docx.table import Table
    from docx.text.paragraph import Paragraph

    document = Document(str(path))
    lines: list[str] = []
    for child in document.element.body.iterchildren():
        if child.tag.endswith("}p"):
            text = Paragraph(child, document).text.strip()
            if text:
                lines.append(text)
        elif child.tag.endswith("}tbl"):
            for row in Table(child, document).rows:
                cells: list[str] = []
                for cell in row.cells:
                    text = cell.text.strip()
                    if text and (not cells or cells[-1] != text):
                        cells.append(text)
                if cells:
                    lines.append(" | ".join(cells))
    return "\n".join(lines)


@router.get("/docs/{doc_id}/file")
def doc_file(doc_id: str, text: bool = False):
    """原文。PDF、Markdown 原样返回；Word 在 text=1 时转成纯文本。"""
    try:
        doc, path = source_file(doc_id)
    except KbError as exc:
        return _fail(exc)
    if text and path.suffix.lower() == ".docx":
        try:
            body = _docx_text(path)
        except Exception:
            return _fail(KbError("Word 原文无法打开", status=400, code="bad_file"))
        return PlainTextResponse(body)
    media = _FILE_TYPES.get(path.suffix.lower(), "application/octet-stream")
    return FileResponse(
        path,
        media_type=media,
        filename=doc.get("filename") or path.name,
        content_disposition_type="inline",
    )


@router.delete("/docs/{doc_id}")
def destroy(doc_id: str):
    try:
        remove(doc_id)
    except KbError as exc:
        return _fail(exc)
    return {"ok": True}


@router.get("/review")
def review(status: str = Query("pending")):
    try:
        rows = list_documents({"status": status})
    except Exception as exc:
        return _missing(exc)
    return {"items": [_row(row) for row in rows]}


@router.post("/docs/{doc_id}/approve")
def do_approve(doc_id: str):
    try:
        return _row(approve(doc_id))
    except KbError as exc:
        return _fail(exc)


@router.post("/docs/{doc_id}/reject")
def do_reject(doc_id: str, reason: str = Form("")):
    try:
        return _row(reject(doc_id, reason))
    except KbError as exc:
        return _fail(exc)


@router.get("/eval/latest")
def eval_latest():
    try:
        with db.connect() as conn:
            row = conn.execute(
                "SELECT ragas, recall, created_at FROM kb_eval_run ORDER BY created_at DESC LIMIT 1"
            ).fetchone()
    except Exception as exc:
        return _missing(exc)
    if row is None:
        return {"ragas": None, "recall": None, "created_at": ""}
    created = row[2]
    return {
        "ragas": row[0],
        "recall": row[1],
        "created_at": created.isoformat() if hasattr(created, "isoformat") else "",
    }


@router.post("/eval/run")
def eval_run():
    """手动触发一次离线评估。异步执行，立即返回；跑完结果进 kb_eval_run。"""
    global _eval_running, _eval_error
    if _eval_running:
        return JSONResponse(
            status_code=409,
            content={"detail": {"code": "eval_running", "message": "评估正在运行中，请稍后再看结果"}},
        )
    _eval_error = None
    _eval_running = True
    threading.Thread(target=_run_eval_background, daemon=True).start()
    return {"started": True, "message": "评估已在后台启动，跑完刷新可见最新结果"}


@router.get("/eval/status")
def eval_status():
    """评估运行状态：是否在跑、最近一次错误。"""
    return {"running": _eval_running, "error": _eval_error}


def _missing(exc: Exception):
    if "kb_document" in str(exc) or "kb_eval_run" in str(exc):
        return JSONResponse(
            status_code=503,
            content={"detail": {"code": "missing_table", "message": "请先运行 python -m scripts.migrate"}},
        )
    raise exc
