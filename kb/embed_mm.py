"""企业知识库的多模态嵌入。只出独立向量，不把图和文字放进同一次 content。"""
from __future__ import annotations

import base64
import logging
from typing import Any

import config
from kb.common import KbError

log = logging.getLogger("kb.embed")


def embed_text(text: str) -> list[float]:
    body = (text or "").strip()
    if not body:
        raise KbError("没有可嵌入的文本", code="empty_text")
    return _call([{"text": body[: config.KB_CHUNK_CHARS]}])


def embed_image_bytes(raw: bytes, *, mime: str = "image/png") -> list[float]:
    if not raw:
        raise KbError("图片是空的", code="empty_image")
    if len(raw) > 10 * 1024 * 1024:
        raise KbError("单张图超过 10MB，嵌入接口不接受", code="image_too_large")
    encoded = base64.b64encode(raw).decode("ascii")
    return _call([{"image": f"data:{mime};base64,{encoded}"}])


def _call(content: list[dict[str, str]]) -> list[float]:
    if not config.DASHSCOPE_API_KEY:
        raise KbError("未配置 DASHSCOPE_API_KEY", status=503, code="no_embed_key")
    from dashscope import MultiModalEmbedding

    resp = MultiModalEmbedding.call(
        model=config.KB_EMBED_MODEL,
        input=content,
        api_key=config.DASHSCOPE_API_KEY,
        dimension=config.KB_EMBED_DIM,
    )
    status = getattr(resp, "status_code", None)
    if status not in (200, None):
        message = getattr(resp, "message", "") or str(resp)
        raise KbError(f"嵌入失败：{message}", status=502, code="embed_failed")
    output: Any = getattr(resp, "output", None) or {}
    if not isinstance(output, dict):
        output = dict(output) if output else {}
    rows = output.get("embeddings") or []
    if not rows:
        raise KbError("嵌入没有返回向量", status=502, code="embed_empty")
    row = rows[0]
    kind = row.get("type") or ""
    if kind == "fused":
        raise KbError("嵌入返回了融合向量，本库只收独立向量", status=502, code="fused_vector")
    vector = row.get("embedding") or []
    if len(vector) != config.KB_EMBED_DIM:
        raise KbError(
            f"嵌入维度是 {len(vector)}，集合要求 {config.KB_EMBED_DIM}",
            status=502,
            code="embed_dim",
        )
    log.info("嵌入 type=%s dim=%d", kind or "?", len(vector))
    return [float(x) for x in vector]
