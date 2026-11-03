"""给图片写一段中文描述。正文行继续只存正文；描述另起一行，用文本向量入库。"""
from __future__ import annotations

import base64

import config
from kb.common import KbError

_CONTEXT_CHARS = 500
_CAPTION_CHARS = 300


def caption_bytes(
    raw: bytes,
    *,
    mime: str,
    heading: str,
    filename: str,
    prev_text: str = "",
    next_text: str = "",
) -> str:
    if not config.DASHSCOPE_API_KEY:
        raise KbError("未配置 DASHSCOPE_API_KEY", status=503, code="no_caption_key")
    from dashscope import MultiModalConversation

    prev = (prev_text or "")[:_CONTEXT_CHARS]
    nxt = (next_text or "")[:_CONTEXT_CHARS]
    encoded = base64.b64encode(raw).decode("ascii")
    resp = MultiModalConversation.call(
        model=config.KB_CAPTION_MODEL,
        api_key=config.DASHSCOPE_API_KEY,
        messages=[{
            "role": "user",
            "content": [
                {"image": f"data:{mime};base64,{encoded}"},
                {"text": _prompt(heading, prev, nxt)},
            ],
        }],
    )
    status = getattr(resp, "status_code", None)
    if status not in (200, None):
        message = getattr(resp, "message", "") or "描述图片失败"
        raise KbError(str(message), status=502, code="caption_failed")
    return _finish(_text_of(resp), filename)


def _prompt(heading: str, prev: str, nxt: str) -> str:
    """前后文都有、只有一边、两边都没有，三档提示。没有坐标的 PDF 图走第三档，只带章节。"""
    head = f"章节：{heading}。\n" if heading else ""
    limit = "描述内容长度最好不超过300个汉字。"
    if prev and nxt:
        body = (
            f"前文内容: {prev}\n后文内容: {nxt}\n\n"
            f"请根据以上上下文和图片内容，生成对该图片的简洁描述，{limit}\n"
            "注意：图片可能与前文、后文或两者都相关，请综合分析。"
        )
    elif prev:
        body = (
            f"前文内容: {prev}\n\n"
            f"请根据以上前文和图片内容，生成对该图片的简洁描述，{limit}\n"
            "注意：图片可能与前文内容相关，请结合分析。"
        )
    elif nxt:
        body = (
            f"后文内容: {nxt}\n\n"
            f"请根据以上后文和图片内容，生成对该图片的简洁描述，{limit}\n"
            "注意：图片可能与后文内容相关，请结合分析。"
        )
    else:
        body = f"请描述这张图片的内容，生成对该图片的简洁描述，{limit}"
    return head + body


def _finish(text: str, filename: str) -> str:
    cleaned = (text or "").strip()
    if not cleaned:
        return f"图:{filename}"
    return cleaned[:_CAPTION_CHARS]


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
