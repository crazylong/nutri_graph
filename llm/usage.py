"""一次请求的记账和 span。

llm 行是账本。node / tool 行只在 TRACE_ENABLED 时写。
父节点是进入 span 之前保存的那个 id。写行在把当前 span 复位之后。
"""
from __future__ import annotations

import logging
import time
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

_SUMMARY_PURPOSE = "trace_summary"
# 送给摘要模型的原文上限。再长就保留头尾，避免一次摘要把上下文撑满。
_SUMMARY_SOURCE_MAX = 8000
_SUMMARY_STORE_MAX = 500
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


def _text_max() -> int:
    limit = int(config.TRACE_TEXT_MAX)
    return limit if limit > 0 else 2048


def _clip(text: str) -> str:
    text = text or ""
    limit = _text_max()
    if len(text) > limit:
        return text[:limit]
    return text


def _summary_on() -> bool:
    """摘要开关只有在追踪正文也打开时才算数。"""
    return bool(config.TRACE_ENABLED and config.TRACE_SUMMARY_ENABLED)


def _shrink_source(text: str) -> str:
    if len(text) <= _SUMMARY_SOURCE_MAX:
        return text
    head = _SUMMARY_SOURCE_MAX // 2
    tail = _SUMMARY_SOURCE_MAX - head
    return text[:head] + "\n…（中间已略）\n" + text[-tail:]


def _make_summary(full_in: str, full_out: str) -> tuple[str, str, int, int, int]:
    """返回 (摘要, 模型名, prompt_tokens, completion_tokens, latency_ms)。"""
    from langchain_core.messages import HumanMessage

    from llm.middleware import _usage
    from llm.router import pool_chain

    blocks = []
    if full_in:
        blocks.append("入参：\n" + _shrink_source(full_in))
    if full_out:
        blocks.append("出参：\n" + _shrink_source(full_out))
    prompt = (
        "把下面超长的模型调用原文压成摘要，只保留关键事实，不要发挥。"
        "每一侧不超过 200 字。没有给出的一侧不要写。\n\n"
        + "\n\n".join(blocks)
    )
    started = time.perf_counter()
    resp = pool_chain(temperature=0, max_tokens=400, enable_thinking=False).invoke(
        [HumanMessage(content=prompt)]
    )
    latency_ms = int((time.perf_counter() - started) * 1000)
    content = getattr(resp, "content", resp)
    text = content if isinstance(content, str) else str(content or "")
    text = text.strip()[:_SUMMARY_STORE_MAX]
    meta = getattr(resp, "response_metadata", None) or {}
    pin, pout = _usage(resp if not isinstance(resp, str) else None)
    model = str(meta.get("model_name") or meta.get("model") or "")
    return text, model, pin, pout, latency_ms


def _write_summary(span_id: str, full_in: str, full_out: str) -> None:
    """补上摘要。打码若已经写过占位，就不要再用原文摘要盖回去。"""
    from kg.sensitive import REDACT

    try:
        text, model, pin, pout, latency_ms = _make_summary(full_in, full_out)
    except Exception:
        log.warning("追踪摘要失败 span=%s", span_id, exc_info=True)
        return
    if not text:
        return
    try:
        import db

        with db.connect() as conn:
            row = conn.execute(
                "SELECT input, output, summary FROM llm_call_log WHERE span_id = %s",
                (span_id,),
            ).fetchone()
            if row is not None and not row[2]:
                stored = REDACT if row[0] == REDACT or row[1] == REDACT else text
                conn.execute(
                    "UPDATE llm_call_log SET summary = %s WHERE span_id = %s AND summary = ''",
                    (stored, span_id),
                )
    except Exception:
        log.warning("写入追踪摘要失败 span=%s", span_id, exc_info=True)
        return
    parent = _current_span.set(span_id)
    try:
        record(
            kind="llm",
            model=model,
            input="",
            output="",
            prompt_tokens=pin,
            completion_tokens=pout,
            latency_ms=latency_ms,
            purpose_name=_SUMMARY_PURPOSE,
        )
    finally:
        _current_span.reset(parent)


def _maybe_summarize(span_id: str, purpose: str, full_in: str, full_out: str) -> None:
    if not span_id or not _summary_on() or purpose == _SUMMARY_PURPOSE:
        return
    limit = _text_max()
    over_in = full_in if len(full_in) > limit else ""
    over_out = full_out if len(full_out) > limit else ""
    if not over_in and not over_out:
        return
    from utils.spawn import submit_background

    submit_background("trace-summary", _write_summary, span_id, over_in, over_out)


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
    full_in, full_out = text_in, text_out
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
        return
    _maybe_summarize(span_id, pur or "", full_in, full_out)


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
                    "UPDATE llm_call_log SET input = %s, summary = %s WHERE rid = %s",
                    (REDACT, REDACT, rid),
                )
            else:
                conn.execute(
                    "UPDATE llm_call_log SET input = %s, output = %s, summary = %s WHERE rid = %s",
                    (REDACT, REDACT, REDACT, rid),
                )
    except Exception as exc:  # noqa: BLE001
        log.warning("按 rid 打码失败：%s", exc)
