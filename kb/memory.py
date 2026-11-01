"""知识库会话。表和滚动摘要沿用食疗的 ShortTermMemory，用户号加 kb: 前缀，不进食疗侧栏。"""
from __future__ import annotations

import logging
import re

import config
from kb.common import KbError
from memory.short_term import ShortTermMemory

log = logging.getLogger("kb.memory")

_CLIENT_RE = re.compile(r"^[A-Za-z0-9_-]{8,64}$")
_SUMMARY_PROMPT = """\
你是企业知识库的记忆整理助手。把「已有摘要」和「新增对话」合并成一份摘要。

固定三段，不要自由发挥：
1) 用户在查的制度或系统（≤5 条）
2) 已给出的回答要点（≤5 条，每条 ≤20 字）
3) 用户问过的问题清单

只输出合并后的摘要正文，不要小标题以外的解释。
"""


def check_client(value: str) -> str:
    text = (value or "").strip()
    if not _CLIENT_RE.fullmatch(text):
        raise KbError("会话身份无效", code="bad_client")
    return text


def user_id(client_id: str) -> str:
    return "kb:" + check_client(client_id)


def open_session(client_id: str, session_id: str | None) -> str:
    memory = ShortTermMemory()
    owner = user_id(client_id)
    if session_id:
        if not memory.owns(owner, session_id):
            raise KbError("会话不存在", status=404, code="session_not_found")
        return str(session_id)
    return memory.create_session(owner)


def history_of(session_id: str) -> str:
    if not config.MEMORY_ENABLED:
        return ""
    return ShortTermMemory().load_context(session_id)


def remember(session_id: str, question: str, answer: str, hits: list[dict]) -> None:
    if not config.MEMORY_ENABLED:
        return
    ShortTermMemory().save_round(session_id, question, answer, {"hits": hits})
    _defer_compress(session_id)


def messages(client_id: str, session_id: str) -> list[dict]:
    rows = ShortTermMemory().list_messages(user_id(client_id), session_id)
    if rows is None:
        raise KbError("会话不存在", status=404, code="session_not_found")
    out = []
    for row in rows:
        item = {"role": row["role"], "content": row["content"]}
        if row["role"] == "assistant":
            item["hits"] = row.get("hits") or []
        out.append(item)
    return out


def _kb_summarize(prev: str, inc: str) -> str:
    if not config.DASHSCOPE_API_KEY:
        body = "\n".join(inc.splitlines()[-20:])
        return ((prev + "\n" + body) if prev else body)[:4000]
    from llm import invoke_model

    resp = invoke_model(
        f"{_SUMMARY_PROMPT}\n已有摘要：\n{prev or '（无）'}\n\n新增对话：\n{inc}",
        enable_thinking=False,
    )
    return str(getattr(resp, "content", "") or "").strip()[:4000]


def _defer_compress(session_id: str) -> None:
    if not config.MEMORY_SUMMARY_ENABLED:
        return

    def _run() -> None:
        from llm.usage import purpose

        try:
            with purpose("summary"):
                ShortTermMemory().compress_if_needed(session_id, summarize_fn=_kb_summarize)
        except Exception as exc:  # noqa: BLE001
            log.warning("知识库滚动摘要失败（不影响回答）：%s", exc)

    from utils.spawn import submit_background

    submit_background("kb-session-compress", _run)
