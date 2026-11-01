"""企业知识库的后台：查询、审核。整组要管理员令牌。"""
from __future__ import annotations

from fastapi import APIRouter, Depends, File, Form, Query, UploadFile
from fastapi.responses import JSONResponse

from api.deps import require_admin
from kb.common import KbError
from kb.publish import add_document, approve, edit_document, reject, remove
from kb.store import list_documents, list_tenants

import db

router = APIRouter(prefix="/admin/kb", tags=["admin-kb"], dependencies=[Depends(require_admin)])


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
        )
    except KbError as exc:
        return _fail(exc)
    return _row(row)


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


def _missing(exc: Exception):
    if "kb_document" in str(exc) or "kb_eval_run" in str(exc):
        return JSONResponse(
            status_code=503,
            content={"detail": {"code": "missing_table", "message": "请先运行 python -m scripts.migrate"}},
        )
    raise exc
