"""把粘在一起的食材名拆开。规则切词切不开「猪脚天麻当归」这种中间没有停顿的片段。"""
from __future__ import annotations

import json
import logging
import threading

log = logging.getLogger("kg.segment")

SPLIT_TIMEOUT_SEC = 3


def keep_substrings(fragment: str, terms: list[str], limit: int = 4) -> list[str]:
    """只留下原文里的连续子串。单字丢掉，「天」「麻」过不了这道。"""
    out: list[str] = []
    for raw in terms:
        term = (raw or "").strip()
        if len(term) < 2 or term not in fragment or term in out:
            continue
        out.append(term)
        if len(out) >= limit:
            break
    return out


def _parse_terms(text: str) -> list[str]:
    raw = (text or "").strip()
    if raw.startswith("```"):
        raw = raw.strip("`")
        if raw.lower().startswith("json"):
            raw = raw[4:]
        raw = raw.strip()
    try:
        data = json.loads(raw)
    except json.JSONDecodeError:
        return []
    if not isinstance(data, list):
        return []
    return [str(x) for x in data]


def _default_llm(fragment: str) -> str:
    from langchain_core.messages import HumanMessage

    from llm.router import pool_chain

    prompt = (
        "从下面这段文字里拆出食材名称，只输出 JSON 数组。"
        "每个名称必须是原文里连续出现的子串，不要解释，不要补充原文里没有的词。\n"
        f"{fragment}"
    )
    chain = pool_chain(temperature=0, max_tokens=120, enable_thinking=False)
    msg = chain.invoke([HumanMessage(content=prompt)])
    return msg.content if hasattr(msg, "content") else str(msg)


def split_compound(fragment: str, llm=None) -> list[str]:
    """拆一个残余片段。失败、超时、没有合法子串都返回空列表，调用方保持整段。"""
    if not fragment:
        return []
    caller = llm if llm is not None else _default_llm
    holder: dict = {}

    def run() -> None:
        try:
            holder["value"] = caller(fragment)
        except Exception as exc:  # noqa: BLE001 —— 模型失败不能让 /chat 500
            holder["error"] = exc

    thread = threading.Thread(target=run, daemon=True)
    thread.start()
    thread.join(SPLIT_TIMEOUT_SEC)
    if thread.is_alive():
        log.info("食材切分超时 fragment=%s terms=[] timeout=true", fragment)
        return []
    if holder.get("error") is not None:
        log.info(
            "食材切分失败 fragment=%s terms=[] timeout=false error=%s",
            fragment, holder["error"],
        )
        return []
    value = holder.get("value")
    terms = value if isinstance(value, list) else _parse_terms(str(value or ""))
    kept = keep_substrings(fragment, [str(t) for t in terms])
    log.info("食材切分 fragment=%s terms=%s timeout=false", fragment, kept)
    return kept
