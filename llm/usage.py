"""一次请求的记账和 span。

llm 行是账本。node / tool 行只在 TRACE_ENABLED 时写。
父节点是进入 span 之前保存的那个 id。写行在把当前 span 复位之后。
"""
from __future__ import annotations

import logging
import uuid
from contextlib import contextmanager
from contextvars import ContextVar

import config

log = logging.getLogger("llm.usage")

_rid: ContextVar[str] = ContextVar("rid", default="")
_user_id: ContextVar[str] = ContextVar("usage_user", default="")
_session_id: ContextVar[str] = ContextVar("usage_session", default="")
_purpose: ContextVar[str] = ContextVar("usage_purpose", default="unknown")
_current_span: ContextVar[str] = ContextVar("current_span", default="")
_redact: ContextVar[bool] = ContextVar("trace_redact", default=False)
_keep_output: ContextVar[bool] = ContextVar("trace_keep_output", default=False)

_TRUNC = 2048
_price_warned: set[str] = set()


class _Box:
    def __init__(self, text: str):
        self.input = text
        self.output = ""
        self.ok = True
        self.model = ""


def set_rid(value: str):
    # 每个请求一条新的 rid。打码标记只属于上一个请求，不能跟着线程留下来。
    _redact.set(False)
    _keep_output.set(False)
    return _rid.set(value or "")


def reset_rid(token) -> None:
    _rid.reset(token)


def current_rid() -> str:
    return _rid.get()


def bind_identity(user_id: str, session_id: str | None) -> None:
    _user_id.set(user_id or "")
    _session_id.set(session_id or "")


@contextmanager
def purpose(name: str):
    token = _purpose.set(name or "unknown")
    try:
        yield
    finally:
        _purpose.reset(token)


def mark_redact(*, keep_output: bool) -> None:
    _redact.set(True)
    _keep_output.set(bool(keep_output))


def price_yuan(model: str, prompt_tokens: int, completion_tokens: int) -> float:
    pair = config.LLM_PRICE_MAP.get(model or "")
    if pair is None:
        if model and model not in _price_warned:
            _price_warned.add(model)
            log.warning("LLM_PRICE_MAP 没有模型 %s，这笔费用按 0 计", model)
        return 0.0
    yuan_in, yuan_out = pair
    return (max(prompt_tokens, 0) / 1000.0) * yuan_in + (max(completion_tokens, 0) / 1000.0) * yuan_out


def _clip(text: str) -> str:
    text = text or ""
    if len(text) > _TRUNC:
        return text[:_TRUNC]
    return text


def _redacted_text(text: str, *, is_output: bool) -> str:
    if not _redact.get():
        return text
    if is_output and _keep_output.get():
        return text
    from kg.sensitive import REDACT

    return REDACT


def _insert(span_id: str, parent: str, kind: str, node: str, model: str, pur: str,
            text_in: str, text_out: str, prompt_tokens: int, completion_tokens: int,
            latency_ms: int, ok: bool) -> None:
    if kind != "llm" and not config.TRACE_ENABLED:
        return
    if kind == "llm" and not config.TRACE_ENABLED:
        text_in, text_out = "", ""
    else:
        text_in = _redacted_text(text_in, is_output=False)
        text_out = _redacted_text(text_out, is_output=True)
    try:
        import db

        with db.connect() as conn:
            conn.execute(
                """
                INSERT INTO llm_call_log (
                    rid, span_id, parent_span_id, kind, node, user_id, session_id,
                    model, purpose, input, output, prompt_tokens, completion_tokens,
                    latency_ms, ok
                ) VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
                """,
                (
                    _rid.get(),
                    span_id,
                    parent,
                    kind,
                    node,
                    _user_id.get(),
                    _session_id.get() or None,
                    model or "",
                    pur or "unknown",
                    _clip(text_in),
                    _clip(text_out),
                    int(prompt_tokens or 0),
                    int(completion_tokens or 0),
                    int(latency_ms or 0),
                    bool(ok),
                ),
            )
    except Exception as exc:  # noqa: BLE001 —— 记账失败不能挡住回答
        log.warning("写入 llm_call_log 失败：%s", exc)


def record(
    *,
    kind: str = "llm",
    node: str = "",
    model: str = "",
    input: str = "",
    output: str = "",
    prompt_tokens: int = 0,
    completion_tokens: int = 0,
    latency_ms: int = 0,
    ok: bool = True,
    purpose_name: str | None = None,
) -> None:
    pur = purpose_name if purpose_name is not None else _purpose.get()
    stored_node = node or (pur if kind == "llm" else "")
    _insert(
        uuid.uuid4().hex[:16],
        _current_span.get(),
        kind,
        stored_node,
        model,
        pur,
        input or "",
        output or "",
        prompt_tokens,
        completion_tokens,
        latency_ms,
        ok,
    )


@contextmanager
def span(kind: str, node: str, *, input: str = ""):
    """包住一整段工作。复位之后才写这一行，父节点用进入前保存的值。"""
    box = _Box(input)
    sid = uuid.uuid4().hex[:16]
    parent = _current_span.get()
    token = _current_span.set(sid)
    try:
        yield box
    except Exception:
        box.ok = False
        raise
    finally:
        _current_span.reset(token)
        pur = _purpose.get()
        _insert(
            sid, parent, kind, node, box.model, pur,
            box.input, box.output, 0, 0, 0, box.ok,
        )


def redact_stored(rid: str, *, keep_output: bool) -> None:
    if not rid:
        return
    from kg.sensitive import REDACT

    try:
        import db

        with db.connect() as conn:
            if keep_output:
                conn.execute(
                    "UPDATE llm_call_log SET input = %s WHERE rid = %s",
                    (REDACT, rid),
                )
            else:
                conn.execute(
                    "UPDATE llm_call_log SET input = %s, output = %s WHERE rid = %s",
                    (REDACT, REDACT, rid),
                )
    except Exception as exc:  # noqa: BLE001
        log.warning("按 rid 打码失败：%s", exc)
