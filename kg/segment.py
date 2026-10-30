"""从用户原句里识别食材。不靠停顿词表判断哪一段是食物。"""
from __future__ import annotations

import json
import logging
import threading

log = logging.getLogger("kg.segment")

SPLIT_TIMEOUT_SEC = 8


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


def _default_llm(question: str) -> str:
    from langchain_core.messages import HumanMessage

    from llm.router import pool_chain

    prompt = (
        "从下面这句用户问题里找出食材名称，只输出 JSON 数组。"
        "只收食物、食材，不要病症、功效和问法。"
        "每个名称必须是原文里连续出现的子串。"
        "没有食材就输出 []。不要解释，不要补充原文里没有的词。\n"
        f"{question}"
    )
    chain = pool_chain(temperature=0, max_tokens=120, enable_thinking=False)
    msg = chain.invoke([HumanMessage(content=prompt)])
    return msg.content if hasattr(msg, "content") else str(msg)


def extract_foods(question: str, llm=None) -> list[str]:
    """从原句识别食材。失败、超时、没有合法子串都返回空列表。

    空列表表示这句没有可登记的食材。调用方不要把原句或残句登记进去。
    """
    if not question:
        return []
    caller = llm if llm is not None else _default_llm
    holder: dict = {}

    def run() -> None:
        try:
            holder["value"] = caller(question)
        except Exception as exc:  # noqa: BLE001 —— 模型失败不能让 /chat 500
            holder["error"] = exc

    thread = threading.Thread(target=run, daemon=True)
    thread.start()
    thread.join(SPLIT_TIMEOUT_SEC)
    if thread.is_alive():
        log.info("食材识别超时 question=%s terms=[] timeout=true", question)
        return []
    if holder.get("error") is not None:
        log.info(
            "食材识别失败 question=%s terms=[] timeout=false error=%s",
            question, holder["error"],
        )
        return []
    value = holder.get("value")
    terms = value if isinstance(value, list) else _parse_terms(str(value or ""))
    kept = keep_substrings(question, [str(t) for t in terms])
    log.info("食材识别 question=%s terms=%s timeout=false", question, kept)
    return kept


def split_compound(fragment: str, llm=None) -> list[str]:
    """旧名字。现在和 extract_foods 是同一步：看整段原文，不切完再补。"""
    return extract_foods(fragment, llm=llm)
