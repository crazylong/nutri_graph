"""企业知识库问答。不登录。没带租户不查库。"""
from __future__ import annotations

from pathlib import Path

from fastapi import APIRouter, File, Form, HTTPException, UploadFile
from fastapi.responses import FileResponse, JSONResponse

from kb import memory
from kb.common import ABSTAIN, KbError, check_tenant
from kb.store import get_chunk, list_tenants
from kb.workflow import run_ask

router = APIRouter(tags=["kb"])


def _fail(exc: KbError) -> JSONResponse:
    return JSONResponse(
        status_code=exc.status,
        content={"detail": {"code": exc.code, "message": exc.message}},
    )


@router.get("/kb/tenants")
def tenants():
    try:
        return {"tenants": list_tenants()}
    except Exception as exc:
        return _missing(exc)


@router.post("/kb/ask")
async def ask(
    tenant_id: str = Form(""),
    question: str = Form(""),
    client_id: str = Form(""),
    session_id: str = Form(""),
    image: UploadFile | None = File(None),
):
    tenant = (tenant_id or "").strip()
    if not tenant:
        return {"answer": ABSTAIN, "abstained": True, "hits": []}
    try:
        tenant = check_tenant(tenant)
    except KbError as exc:
        return _fail(exc)
    raw = await image.read() if image is not None else b""
    text = (question or "").strip()
    if not text and not raw:
        raise HTTPException(status_code=400, detail={"code": "empty_question", "message": "请输入问题或上传一张图"})
    mime = (image.content_type if image is not None and raw else "") or "image/png"
    name = image.filename if image is not None else ""
    try:
        sid, history = _session(client_id, session_id)
    except KbError as exc:
        return _fail(exc)
    try:
        result = run_ask(
            tenant_id=tenant,
            question=text,
            image=raw or None,
            image_name=name or "",
            mime=mime,
            history=history,
        )
    except KbError as exc:
        return _fail(exc)
    if sid:
        spoken = text or name or "（上传图片）"
        try:
            memory.remember(sid, spoken, result["answer"], result["hits"])
        except Exception:
            pass
        result["session_id"] = sid
    return result


@router.get("/kb/sessions/{session_id}")
def session_messages(session_id: str, client_id: str = ""):
    try:
        return {"messages": memory.messages(client_id, session_id)}
    except KbError as exc:
        return _fail(exc)


def _session(client_id: str, session_id: str) -> tuple[str, str]:
    if not (client_id or "").strip():
        return "", ""
    sid = memory.open_session(client_id, session_id or None)
    return sid, memory.history_of(sid)


@router.get("/kb/images/{chunk_id}")
def image(chunk_id: str, tenant_id: str = ""):
    row = get_chunk(chunk_id)
    if row is None or row["status"] != "approved" or row["tenant_id"] != tenant_id:
        raise HTTPException(status_code=404, detail={"code": "not_found", "message": "图片不存在"})
    path = Path(row["image_path"] or "")
    if not path.is_file():
        raise HTTPException(status_code=404, detail={"code": "not_found", "message": "图片不存在"})
    return FileResponse(path)


def _missing(exc: Exception) -> JSONResponse:
    if "kb_document" in str(exc):
        return JSONResponse(
            status_code=503,
            content={"detail": {"code": "missing_table", "message": "请先运行 python -m scripts.migrate"}},
        )
    raise exc
