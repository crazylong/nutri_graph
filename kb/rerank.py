"""企业知识库文本提问的精排。返回全部候选的顺序，截断不在这里。"""
from __future__ import annotations

import logging

import httpx

import config

log = logging.getLogger("kb.rerank")

_DOC_CHARS = 500
_TIMEOUT = 3.0


def rerank(question: str, hits: list[dict]) -> list[dict]:
    """按精排结果重排，再把没返回的候选按原顺序接上。

    分数是 relevance_score，不是余弦，不能拿去和 CHUNK_THRESHOLD 比。
    没密钥、池是空的、超时、非 200、结果是空的，都退回原来的整表。
    """
    if not hits or not config.KB_RERANK_ENABLED:
        return list(hits)
    model = config.RERANK_MODEL_POOL[0] if config.RERANK_MODEL_POOL else ""
    if not config.DASHSCOPE_API_KEY or not model:
        return list(hits)
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
                    "top_n": len(hits),
                },
            },
            timeout=_TIMEOUT,
        )
    except Exception as exc:  # noqa: BLE001
        log.warning("精排失败，退回混合检索顺序：%s", exc)
        return list(hits)
    if response.status_code != 200:
        log.warning("精排 HTTP %s，退回混合检索顺序", response.status_code)
        return list(hits)
    data = response.json()
    results = (data.get("output") or {}).get("results") or []
    usage = data.get("usage") or {}
    if not results:
        log.warning("精排结果为空，退回混合检索顺序 usage=%s", usage)
        return list(hits)
    ordered: list[dict] = []
    scores: list[float] = []
    seen: set[int] = set()
    for item in results:
        index = item.get("index")
        if not isinstance(index, int) or index < 0 or index >= len(hits) or index in seen:
            continue
        seen.add(index)
        hit = dict(hits[index])
        score = float(item.get("relevance_score") or 0)
        hit["score"] = score
        ordered.append(hit)
        scores.append(score)
    if not ordered:
        return list(hits)
    for index, hit in enumerate(hits):
        if index not in seen:
            ordered.append(hit)
    log.info("精排分 model=%s scores=%s usage=%s", model, scores, usage)
    return ordered
