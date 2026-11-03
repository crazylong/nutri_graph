"""文本提问走独立文本向量加 BM25。图片提问只走独立图像向量。"""
from __future__ import annotations

import config
from kb import embed_mm, milvus
from kb.common import KbError


def search_question(tenant_id: str, question: str, *, limit: int | None = None) -> list[dict]:
    if not tenant_id:
        raise KbError("没有租户就不查库", code="no_tenant")
    text = (question or "").strip()
    if not text:
        raise KbError("问题是空的", code="empty_question")
    dense = embed_mm.embed_text(text)
    size = _limit(limit)
    return _dedupe(milvus.search_text(tenant_id, text, dense, limit=size))[:size]


def search_picture(tenant_id: str, raw: bytes, mime: str, *, limit: int | None = None) -> list[dict]:
    if not tenant_id:
        raise KbError("没有租户就不查库", code="no_tenant")
    dense = embed_mm.embed_image_bytes(raw, mime=mime)
    size = _limit(limit)
    return _dedupe(milvus.search_image(tenant_id, dense, limit=size))[:size]


def _dedupe(hits: list[dict]) -> list[dict]:
    """同一张图的图像行和描述行只留分高的那条，位置用先出现的那次。

    没有图片路径的正文不合并。去重放在返回前，提问和评测走的是同一条。
    """
    kept: list[dict] = []
    slot: dict[str, int] = {}
    for hit in hits:
        path = (hit.get("image_path") or "").strip()
        if not path:
            kept.append(hit)
            continue
        previous = slot.get(path)
        if previous is None:
            slot[path] = len(kept)
            kept.append(hit)
            continue
        if float(hit.get("score") or 0) > float(kept[previous].get("score") or 0):
            kept[previous] = hit
    return kept


def _limit(limit: int | None) -> int:
    size = config.KB_SEARCH_LIMIT if limit is None else limit
    return max(1, int(size))
