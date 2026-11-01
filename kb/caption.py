"""给图片写一段供 BM25 和回答用的描述。描述不进稠密向量。"""
from __future__ import annotations

import base64

import config
from kb.common import KbError


def caption_bytes(raw: bytes, *, mime: str, heading: str, filename: str) -> str:
    if not config.DASHSCOPE_API_KEY:
        raise KbError("未配置 DASHSCOPE_API_KEY", status=503, code="no_caption_key")
    from dashscope import MultiModalConversation

    encoded = base64.b64encode(raw).decode("ascii")
    hint = f"章节：{heading}。" if heading else ""
    resp = MultiModalConversation.call(
        model=config.KB_CAPTION_MODEL,
        api_key=config.DASHSCOPE_API_KEY,
        messages=[{
            "role": "user",
            "content": [
                {"image": f"data:{mime};base64,{encoded}"},
                {"text": hint + "用两三句中文说明这张图里有什么，保留图中的文字和系统名。"},
            ],
        }],
    )
    status = getattr(resp, "status_code", None)
    if status not in (200, None):
        message = getattr(resp, "message", "") or "描述图片失败"
        raise KbError(str(message), status=502, code="caption_failed")
    text = _text_of(resp).strip()
    if not text:
        return f"图:{filename}"
    return text[:800]


def _text_of(resp) -> str:
    output = getattr(resp, "output", None)
    choices = getattr(output, "choices", None) or (output or {}).get("choices") or []
    if not choices:
        return ""
    message = choices[0].get("message") if isinstance(choices[0], dict) else choices[0].message
    content = message.get("content") if isinstance(message, dict) else message.content
    if isinstance(content, str):
        return content
    parts = []
    for piece in content or []:
        if isinstance(piece, dict) and piece.get("text"):
            parts.append(piece["text"])
        elif isinstance(piece, str):
            parts.append(piece)
    return "\n".join(parts)
