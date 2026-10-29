"""
kg/webanswer.py —— 未收录食材的当轮网页参考

顺序：缓存 → 限流 → 预算 → 搜索 → 裁剪 → 生成 → 再裁剪 → 记日志。
不查图谱，不读画像。任何失败都回到未收录话术，/chat 不因此 500。
"""
from __future__ import annotations

import hashlib
import logging
import time
from typing import Any, Callable

from psycopg.types.json import Jsonb

import config
import db
from kg.normalize import normalize_text
from kg.sensitive import log_hits
from kg.webguard import has_political, is_herbal, trim_sentences
from kg.websearch import WebResult, WebSearchError, search

log = logging.getLogger("kg.webanswer")

_BLOCKED_INTENTS = frozenset({"contra_population", "contra_item", "symptom_remedy"})
_CONFIDENCE_CAP = 0.25


def cache_key(question: str) -> str:
    raw = config.WEB_ANSWER_PROMPT_VERSION + "\x00" + normalize_text(question or "")
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def allowed(state: dict) -> bool:
    """plans 已经为空时，还要过意图、健康自述和「像不像食材」。"""
    if not config.WEB_SEARCH_ENABLED:
        return False
    intents = state.get("_intents") or ["pair"]
    allowed_intents = set(config.WEB_ANSWER_INTENTS)
    if any(intent in _BLOCKED_INTENTS or intent not in allowed_intents for intent in intents):
        return False
    from graph.boundary import mentions_health_constraint

    if mentions_health_constraint(state.get("question") or ""):
        return False
    from kg.request import is_plausible_food

    terms = state.get("_unresolved") or []
    if not any(is_plausible_food(term) for term in terms):
        return False
    return True


def _search(query: str) -> list[WebResult]:
    return search(query)


def _generate(prompt: str) -> dict:
    if not config.DASHSCOPE_API_KEY:
        raise WebSearchError("no_model_key")
    from llm import LLMUnavailableError, invoke_model

    try:
        resp = invoke_model(prompt, enable_thinking=False)
    except LLMUnavailableError as exc:
        raise WebSearchError("model_unavailable") from exc
    content = getattr(resp, "content", resp)
    return _parse_model(content)


def _parse_model(content: str) -> dict:
    import json

    try:
        obj = json.loads(content)
    except (json.JSONDecodeError, TypeError, ValueError):
        obj = {"answer": content if isinstance(content, str) else ""}
    if not isinstance(obj, dict):
        obj = {"answer": str(content)}
    return obj


def _clamp(value: Any) -> float:
    try:
        number = float(value)
    except (TypeError, ValueError):
        number = 0.0
    return min(_CONFIDENCE_CAP, max(0.0, number))


def _host_sources(rows: list[WebResult]) -> list[dict[str, str]]:
    out = []
    for row in rows:
        if not (row.content or row.title):
            continue
        out.append({
            "title": row.title or row.site or "网页",
            "site": row.site,
            "url": row.url,
        })
    return out


def _registration_line(results) -> str:
    from graph.nodes import _visible_requests

    named = []
    ids = []
    for item in _visible_requests(results):
        if not item.request_id:
            continue
        named.append(f"「{item.term or item.food_key}」")
        ids.append(f"#{item.request_id}")
    if not named:
        return "这些食材还没进知识库。"
    return f"{'、'.join(named)}还没进知识库（已登记 {' / '.join(ids)}）。"


def _source_block(sources: list[dict[str, str]]) -> str:
    lines = [f"· {item['title']} — {item['site']}" for item in sources if item.get("title")]
    return (
        f"参考来源（{len(lines)} 个站点）：\n" + "\n".join(lines)
        if lines else "参考来源：无"
    )


def _wrap(head: str, body: str, sources: list[dict[str, str]]) -> str:
    return (
        f"{head}\n"
        "以下是网络检索的结果，未经审核，不能作为食疗依据：\n\n"
        f"{body.strip()}\n\n"
        f"{_source_block(sources)}\n\n"
        "（提示：以上内容来自公开网页，未经人工审核，不能替代医生或营养师意见。）"
    )


def _fallback(results, *, web: dict | None = None) -> dict:
    from graph.nodes import _food_payload, _uncatalogued_message

    return {
        "answer": _uncatalogued_message(results),
        "basis": [],
        "gaps": [f"图谱未收录：{item.food_key or item.term}" for item in results] or ["未能识别食材"],
        "confidence": 0.0,
        "_graph_hit": False,
        "_degraded": False,
        "_food_requests": _food_payload(results),
        "_web_answer": web or {"used": False, "sources": [], "blocked": ""},
    }


def _success(results, body: str, sources: list[dict], confidence: float, blocked: str = "") -> dict:
    from graph.nodes import _food_payload

    return {
        "answer": _wrap(_registration_line(results), body, sources),
        "basis": [],
        "gaps": [f"图谱未收录：{item.food_key or item.term}" for item in results if item.food_key or item.term],
        "confidence": _clamp(confidence),
        "_graph_hit": False,
        "_degraded": False,
        "_food_requests": _food_payload(results),
        "_web_answer": {"used": True, "sources": sources, "blocked": blocked},
    }


def _filter(rows: list[WebResult]) -> tuple[list[WebResult], list, int]:
    """政治类整条丢。其余按句裁。原始片段短于 40 字的不用。返回 (留下的, 命中, 原始条数)。"""
    hits = []
    kept: list[WebResult] = []
    for row in rows:
        political = has_political(row.content) or has_political(row.title)
        if political:
            hits.extend(political)
            continue
        if len(row.content or "") < config.WEB_ANSWER_MIN_CHARS:
            continue
        trimmed, sent_hits = trim_sentences(row.content)
        hits.extend(sent_hits)
        if not trimmed:
            continue
        kept.append(WebResult(title=row.title, content=trimmed, url=row.url, site=row.site))
    return kept, hits, len(rows)


def _read_cache(conn, key: str):
    return conn.execute(
        "SELECT answer, sources, hit_count FROM web_answer_cache "
        "WHERE query_hash = %s AND expires_at > now()",
        (key,),
    ).fetchone()


def _touch_cache(conn, key: str) -> None:
    conn.execute(
        "UPDATE web_answer_cache SET hit_count = hit_count + 1 WHERE query_hash = %s",
        (key,),
    )


def _write_cache(conn, key: str, question: str, answer: str, sources: list, hours: int) -> None:
    conn.execute(
        """
        INSERT INTO web_answer_cache
            (query_hash, question, answer, sources, provider, hit_count, expires_at)
        VALUES (%s, %s, %s, %s, %s, 0, now() + (%s * interval '1 hour'))
        ON CONFLICT (query_hash) DO UPDATE SET
            question = EXCLUDED.question,
            answer = EXCLUDED.answer,
            sources = EXCLUDED.sources,
            provider = EXCLUDED.provider,
            expires_at = EXCLUDED.expires_at
        """,
        (
            key, question, answer, Jsonb(sources), config.WEB_SEARCH_PROVIDER, hours,
        ),
    )


def _count_attempts(conn, where: str, params: tuple) -> int:
    row = conn.execute(
        f"SELECT count(*) FROM web_search_log WHERE {where} "
        "AND blocked NOT IN ('budget', 'rate', 'disabled') "
        "AND error <> 'no_key'",
        params,
    ).fetchone()
    return int(row[0]) if row else 0


def _log(conn, **fields) -> None:
    conn.execute(
        """
        INSERT INTO web_search_log
            (user_id, query_hash, provider, ok, result_count, kept_count,
             blocked, error, elapsed_ms)
        VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s)
        """,
        (
            fields.get("user_id", ""),
            fields["query_hash"],
            fields.get("provider", ""),
            fields.get("ok", False),
            fields.get("result_count", 0),
            fields.get("kept_count", 0),
            fields.get("blocked", ""),
            (fields.get("error") or "")[:300],
            fields.get("elapsed_ms", 0),
        ),
    )


def answer(
    state: dict,
    *,
    search_fn: Callable[[str], list[WebResult]] | None = None,
    generate_fn: Callable[[str], dict] | None = None,
) -> dict:
    from graph.nodes import _register_unresolved

    question = state.get("question") or ""
    user_id = state.get("user_id") or "anonymous"
    results = _register_unresolved(state)
    state["_registered_this_turn"] = True
    key = cache_key(question)
    started = time.perf_counter()
    search_fn = search_fn or _search
    generate_fn = generate_fn or _generate

    def elapsed() -> int:
        return int((time.perf_counter() - started) * 1000)

    try:
        with db.connect() as conn:
            cached = _read_cache(conn, key)
            if cached is not None:
                _touch_cache(conn, key)
                text, sources, _hits = cached
                sources = sources or []
                if not text:
                    return _fallback(results, web={"used": False, "sources": [], "blocked": "negative"})
                return _success(results, text, sources, _CONFIDENCE_CAP)
    except Exception as exc:
        log.warning("读网页回答缓存失败，继续搜：%s", exc)

    query = question[: config.WEB_ANSWER_MAX_QUESTION]
    try:
        with db.connect() as conn:
            hourly = _count_attempts(
                conn,
                "user_id = %s AND created_at > now() - interval '1 hour'",
                (user_id,),
            )
            if hourly >= config.WEB_ANSWER_USER_HOURLY:
                _log(conn, user_id=user_id, query_hash=key, blocked="rate", elapsed_ms=elapsed())
                return _fallback(results, web={"used": False, "sources": [], "blocked": "rate"})
            used = _count_attempts(
                conn,
                "created_at > now() - interval '1 day'",
                (),
            )
            if used >= config.WEB_ANSWER_DAILY_BUDGET:
                _log(conn, user_id=user_id, query_hash=key, blocked="budget", elapsed_ms=elapsed())
                return _fallback(results, web={"used": False, "sources": [], "blocked": "budget"})
    except Exception as exc:
        log.warning("网页搜索预算检查失败，按不能搜处理：%s", exc)
        return _fallback(results, web={"used": False, "sources": [], "blocked": "error"})

    if search_fn is _search and not config.WEB_SEARCH_API_KEY and config.WEB_SEARCH_PROVIDER != "null":
        _safe_log(user_id, key, ok=False, error="no_key", elapsed_ms=elapsed())
        return _fallback(results, web={"used": False, "sources": [], "blocked": "error"})

    try:
        rows = search_fn(query)
    except WebSearchError as exc:
        _safe_log(
            user_id, key, ok=False, error=str(exc),
            provider=config.WEB_SEARCH_PROVIDER, elapsed_ms=elapsed(),
        )
        return _fallback(results, web={"used": False, "sources": [], "blocked": "error"})
    except Exception as exc:
        log.warning("网页搜索失败：%s", exc)
        _safe_log(
            user_id, key, ok=False, error=f"{type(exc).__name__}: {exc}",
            provider=config.WEB_SEARCH_PROVIDER, elapsed_ms=elapsed(),
        )
        return _fallback(results, web={"used": False, "sources": [], "blocked": "error"})

    kept, hits, raw_count = _filter(rows)
    if hits:
        log_hits("web", hits, source="chat")
    if not kept:
        _safe_log(
            user_id, key, ok=True, result_count=raw_count, kept_count=0,
            blocked="L2", provider=config.WEB_SEARCH_PROVIDER, elapsed_ms=elapsed(),
        )
        _safe_cache(key, question, "", [], config.WEB_ANSWER_NEGATIVE_TTL_H)
        return _fallback(results, web={"used": False, "sources": [], "blocked": "L2"})

    sources = _host_sources(kept)
    snippets = [{"title": row.title, "content": row.content, "site": row.site} for row in kept]
    from graph.boundary import build_web_prompt

    prompt = build_web_prompt(question, snippets, herbal=is_herbal(question))
    remaining_ms = config.WEB_ANSWER_BUDGET_MS - elapsed()
    generated = None
    if remaining_ms > 0:
        try:
            generated = generate_fn(prompt)
        except Exception as exc:
            log.warning("网页回答生成失败，改贴片段：%s", exc)
            generated = None
    if not isinstance(generated, dict) or not str(generated.get("answer") or "").strip():
        body = "\n".join(row.content for row in kept)
        _remember(key, question, body, sources)
        _safe_log(
            user_id, key, ok=True, result_count=raw_count, kept_count=len(kept),
            provider=config.WEB_SEARCH_PROVIDER, elapsed_ms=elapsed(),
        )
        return _success(results, body, sources, _CONFIDENCE_CAP)

    model_text = str(generated.get("answer") or "")
    political = has_political(model_text)
    if political:
        log_hits("web", political, source="chat")
        _safe_log(
            user_id, key, ok=True, result_count=raw_count, kept_count=len(kept),
            blocked="L3", provider=config.WEB_SEARCH_PROVIDER, elapsed_ms=elapsed(),
        )
        return _fallback(results, web={"used": False, "sources": [], "blocked": "L3"})

    trimmed, sent_hits = trim_sentences(model_text)
    if sent_hits:
        log_hits("web", sent_hits, source="chat")
    if not trimmed:
        body = "\n".join(row.content for row in kept)
        blocked = "L3"
    else:
        body = trimmed
        blocked = "L3" if sent_hits else ""
    _remember(key, question, body, sources)
    _safe_log(
        user_id, key, ok=True, result_count=raw_count, kept_count=len(kept),
        blocked=blocked, provider=config.WEB_SEARCH_PROVIDER, elapsed_ms=elapsed(),
    )
    return _success(results, body, sources, generated.get("confidence", _CONFIDENCE_CAP), blocked)


def _remember(key: str, question: str, body: str, sources: list) -> None:
    _safe_cache(key, question, body, sources, config.WEB_ANSWER_CACHE_TTL_H)


def _safe_cache(key: str, question: str, answer: str, sources: list, hours: int) -> None:
    try:
        with db.connect() as conn:
            _write_cache(conn, key, question, answer, sources, hours)
    except Exception as exc:
        log.warning("写网页回答缓存失败：%s", exc)


def _safe_log(user_id: str, key: str, **fields) -> None:
    try:
        with db.connect() as conn:
            _log(conn, user_id=user_id, query_hash=key, **fields)
    except Exception as exc:
        log.warning("写网页搜索日志失败：%s", exc)
