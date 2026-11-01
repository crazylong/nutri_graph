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
    return milvus.search_text(tenant_id, text, dense, limit=_limit(limit))


def search_picture(tenant_id: str, raw: bytes, mime: str, *, limit: int | None = None) -> list[dict]:
    if not tenant_id:
        raise KbError("没有租户就不查库", code="no_tenant")
    dense = embed_mm.embed_image_bytes(raw, mime=mime)
    return milvus.search_image(tenant_id, dense, limit=_limit(limit))


def _limit(limit: int | None) -> int:
    size = config.KB_SEARCH_LIMIT if limit is None else limit
    return max(1, int(size))
