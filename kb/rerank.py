"""企业知识库文本提问的精排。失败就退回混合检索的原顺序，并截到提问条数。"""
from __future__ import annotations

import logging

import httpx

import config

log = logging.getLogger("kb.rerank")

_DOC_CHARS = 500
_TIMEOUT = 3.0


def rerank(question: str, hits: list[dict]) -> list[dict]:
    """按精排结果重排。返回条数不超过 KB_SEARCH_LIMIT。

    分数是 relevance_score，不是余弦，不能拿去和 CHUNK_THRESHOLD 比。
    没密钥、池是空的、超时、非 200、结果是空的，都退回原顺序的前几条。
    """
    kept = hits[: config.KB_SEARCH_LIMIT]
    if not hits or not config.KB_RERANK_ENABLED:
        return kept
    model = config.RERANK_MODEL_POOL[0] if config.RERANK_MODEL_POOL else ""
    if not config.DASHSCOPE_API_KEY or not model:
        return kept
    documents = [((hit.get("text") or "").strip() or "图")[:_DOC_CHARS] for hit in hits]
    try:
        response = httpx.post(
            config.RERANK_BASE_URL,
            headers={
                "Authorization": f"Bearer {config.DASHSCOPE_API_KEY}",
                "Content-Type": "application/json",
            },
            json={
                "model": model,
                "input": {"query": question, "documents": documents},
                "parameters": {
                    "return_documents": False,
                    "top_n": config.KB_SEARCH_LIMIT,
                },
            },
            timeout=_TIMEOUT,
        )
    except Exception as exc:  # noqa: BLE001
        log.warning("精排失败，退回混合检索顺序：%s", exc)
        return kept
    if response.status_code != 200:
        log.warning("精排 HTTP %s，退回混合检索顺序", response.status_code)
        return kept
    data = response.json()
    results = (data.get("output") or {}).get("results") or []
    usage = data.get("usage") or {}
    if not results:
        log.warning("精排结果为空，退回混合检索顺序 usage=%s", usage)
        return kept
    ordered: list[dict] = []
    scores: list[float] = []
    for item in results:
        index = item.get("index")
        if not isinstance(index, int) or index < 0 or index >= len(hits):
            continue
        hit = dict(hits[index])
        score = float(item.get("relevance_score") or 0)
        hit["score"] = score
        ordered.append(hit)
        scores.append(score)
    if not ordered:
        return kept
    log.info("精排分 model=%s scores=%s usage=%s", model, scores, usage)
    return ordered[: config.KB_SEARCH_LIMIT]
