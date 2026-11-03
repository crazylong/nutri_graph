"""企业知识库问答。必须是员工登录，并且属于本次请求的租户。"""
from __future__ import annotations

import logging
import threading
import time
import uuid
from pathlib import Path

from fastapi import APIRouter, Depends, File, Form, HTTPException, UploadFile
from fastapi.responses import FileResponse, JSONResponse, StreamingResponse

import config
import db
from api.deps import get_current_user
from kb import memory
from kb.common import TENANT_RE, KbError
from kb.store import get_chunk
from kb.workflow import run_ask
from memory.short_term import ShortTermMemory

router = APIRouter(tags=["kb"])
log = logging.getLogger("api.kb")
_kb_slots: threading.Semaphore | None = None
_kb_slots_n: int | None = None


def _fail(exc: KbError) -> JSONResponse:
    return JSONResponse(
        status_code=exc.status,
        content={"detail": {"code": exc.code, "message": exc.message}},
    )


def _tenant_for(user: dict, tenant_id: str) -> str:
    tenant = (tenant_id or "").strip()
    if not TENANT_RE.fullmatch(tenant):
        raise HTTPException(
            status_code=422,
            detail={"code": "bad_tenant", "message": "租户只允许字母、数字、下划线，最长 32 位"},
        )
    if not memory.member(user["user_id"], tenant):
        raise HTTPException(
            status_code=403,
            detail={"code": "forbidden", "message": "不能访问这个租户"},
        )
    return tenant


@router.get("/kb/tenants")
def tenants(user: dict = Depends(get_current_user)):
    with db.connect() as conn:
        rows = conn.execute(
            "SELECT tenant_name FROM tenant_user WHERE user_id = %s ORDER BY 1",
            (user["user_id"],),
        ).fetchall()
    return {"tenants": [row[0] for row in rows]}


@router.post("/kb/ask")
async def ask(
    user: dict = Depends(get_current_user),
    tenant_id: str = Form(""),
    question: str = Form(""),
    session_id: str = Form(""),
    image: UploadFile | None = File(None),
):
    tenant = _tenant_for(user, tenant_id)
    raw = await image.read() if image is not None else b""
    text = (question or "").strip()
    if not text and not raw:
        raise HTTPException(status_code=400, detail={"code": "empty_question", "message": "请输入问题或上传一张图"})
    mime = (image.content_type if image is not None and raw else "") or "image/png"
    name = image.filename if image is not None else ""
    try:
        sid, history = _session(memory.owner(user["user_id"], tenant), session_id)
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
        spoken = text
        try:
            memory.remember(sid, spoken, result["answer"], result["hits"], image=raw or None, mime=mime)
        except Exception:
            pass
        result["session_id"] = sid
    return result


@router.get("/kb/sessions")
def kb_sessions(tenant_id: str = "", user: dict = Depends(get_current_user)):
    tenant = _tenant_for(user, tenant_id)
    return {"sessions": memory.list_sessions(memory.owner(user["user_id"], tenant))}


@router.get("/kb/recent")
def kb_recent(tenant_id: str = "", user: dict = Depends(get_current_user)):
    tenant = _tenant_for(user, tenant_id)
    return {"questions": memory.recent_questions(memory.owner(user["user_id"], tenant))}


def _kb_slot() -> threading.Semaphore:
    global _kb_slots, _kb_slots_n
    n = max(int(config.STREAM_MAX_CONCURRENT), 1)
    if _kb_slots is None or _kb_slots_n != n:
        _kb_slots = threading.Semaphore(n)
        _kb_slots_n = n
    return _kb_slots


def _prepare_ask(user: dict, tenant_id: str, question: str, session_id: str, raw: bytes, name: str):
    tenant = _tenant_for(user, tenant_id)
    text = (question or "").strip()
    if not text and not raw:
        raise HTTPException(status_code=400, detail={"code": "empty_question", "message": "请输入问题或上传一张图"})
    sid, history = _session(memory.owner(user["user_id"], tenant), session_id)
    spoken = text
    return (tenant, text, sid, history, spoken), None


async def _sse_with_cancel(bus, stream_id: str, deadline: float):
    from chatbus import iter_sse

    try:
        async for chunk in iter_sse(bus, stream_id, deadline):
            yield chunk
    finally:
        bus.cancel(stream_id)


def _run_kb_stream(
    bus,
    stream_id: str,
    *,
    tenant: str,
    text: str,
    raw: bytes,
    name: str,
    mime: str,
    history: str,
    sid: str,
    spoken: str,
    release_slot: bool,
):
    from kb.streamer import KbStreamer
    from llm.streamctx import ModelStreamBroken, StreamCancelled, bind

    streamer = KbStreamer(
        lambda event, data: bus.push(stream_id, event, data),
        cancelled=lambda: bus.cancelled(stream_id),
    )

    def work():
        try:
            try:
                streamer.status("正在查找")
                with bind(streamer):
                    result = run_ask(
                        tenant_id=tenant,
                        question=text,
                        image=raw or None,
                        image_name=name or "",
                        mime=mime,
                        history=history,
                    )
                streamer.flush()
                if not streamer.sent_text:
                    streamer.text_delta(result.get("answer") or "")
                if sid and not streamer.cancelled():
                    try:
                        memory.remember(
                            sid, spoken, result["answer"], result["hits"],
                            image=raw or None, mime=mime,
                        )
                    except Exception:
                        pass
                payload = {
                    "answer": result.get("answer") or "",
                    "abstained": bool(result.get("abstained")),
                    "hits": result.get("hits") or [],
                }
                if sid:
                    payload["session_id"] = sid
                streamer.result(payload)
                streamer.finish_stream()
            except (StreamCancelled, ModelStreamBroken):
                if not streamer.done:
                    streamer.error("生成中断")
                    streamer.finish_stream()
            except Exception:
                log.exception("知识库流式生成失败")
                if not streamer.done:
                    streamer.error("生成失败")
                    streamer.finish_stream()
        finally:
            if release_slot:
                _kb_slot().release()

    from utils.spawn import spawn

    return spawn(f"kb-{stream_id[:8]}", work)


@router.post("/kb/ask/stream")
async def ask_stream(
    user: dict = Depends(get_current_user),
    tenant_id: str = Form(""),
    question: str = Form(""),
    session_id: str = Form(""),
    image: UploadFile | None = File(None),
):
    raw = await image.read() if image is not None else b""
    name = image.filename if image is not None else ""
    mime = (image.content_type if image is not None and raw else "") or "image/png"
    try:
        prepared, early = _prepare_ask(user, tenant_id, question, session_id, raw, name)
    except KbError as exc:
        return _fail(exc)
    if early is not None:
        return early
    tenant, text, sid, history, spoken = prepared
    from chatbus import open_bus, stream_limit_seconds

    bus = open_bus()
    if bus is None:
        return JSONResponse(status_code=503, content={"error": "流式通道不可用"})
    slots = _kb_slot()
    if not slots.acquire(blocking=False):
        return JSONResponse(
            status_code=503,
            content={"detail": {"code": "stream_busy", "message": "当前回答过多，请稍后再试"}},
        )
    stream_id = uuid.uuid4().hex
    try:
        _run_kb_stream(
            bus,
            stream_id,
            tenant=tenant,
            text=text,
            raw=raw,
            name=name or "",
            mime=mime,
            history=history,
            sid=sid,
            spoken=spoken,
            release_slot=True,
        )
    except Exception:
        slots.release()
        raise
    deadline = time.monotonic() + stream_limit_seconds()
    return StreamingResponse(
        _sse_with_cancel(bus, stream_id, deadline),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "X-Accel-Buffering": "no",
        },
    )


@router.get("/kb/sessions/{session_id}")
def session_messages(session_id: str, tenant_id: str = "", user: dict = Depends(get_current_user)):
    tenant = _tenant_for(user, tenant_id)
    try:
        return {"messages": memory.messages(memory.owner(user["user_id"], tenant), session_id, tenant=tenant)}
    except KbError as exc:
        return _fail(exc)


def _session(owner_id: str, session_id: str) -> tuple[str, str]:
    sid = memory.open_session(owner_id, session_id or None)
    return sid, memory.history_of(sid)


@router.get("/kb/ask-images/{session_id}/{name}")
def ask_image(session_id: str, name: str, tenant_id: str = "", user: dict = Depends(get_current_user)):
    tenant = _tenant_for(user, tenant_id)
    if not ShortTermMemory().owns(memory.owner(user["user_id"], tenant), session_id):
        raise HTTPException(status_code=404, detail={"code": "not_found", "message": "图片不存在"})
    path = memory.ask_image_path(session_id, name)
    if path is None:
        raise HTTPException(status_code=404, detail={"code": "not_found", "message": "图片不存在"})
    return FileResponse(path)


@router.get("/kb/images/{chunk_id}")
def image(chunk_id: str, tenant_id: str = "", user: dict = Depends(get_current_user)):
    tenant = _tenant_for(user, tenant_id)
    row = get_chunk(chunk_id)
    if row is None or row["status"] != "approved" or row["tenant_id"] != tenant:
        raise HTTPException(status_code=404, detail={"code": "not_found", "message": "图片不存在"})
    path = Path(row["image_path"] or "")
    if not path.is_file():
        raise HTTPException(status_code=404, detail={"code": "not_found", "message": "图片不存在"})
    return FileResponse(path)

