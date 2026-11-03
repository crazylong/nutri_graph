"""知识库会话。表和滚动摘要沿用食疗的 ShortTermMemory，用户号加 kb: 前缀，不进食疗侧栏。"""
from __future__ import annotations

import logging

import config
import db
from kb.common import KbError, check_tenant
from memory.short_term import ShortTermMemory

log = logging.getLogger("kb.memory")

_SUMMARY_PROMPT = """\
你是企业知识库的记忆整理助手。把「已有摘要」和「新增对话」合并成一份摘要。

固定三段，不要自由发挥：
1) 用户在查的制度或系统（≤5 条）
2) 已给出的回答要点（≤5 条，每条 ≤20 字）
3) 用户问过的问题清单

只输出合并后的摘要正文，不要小标题以外的解释。
"""


def owner(user_id: str, tenant: str) -> str:
    """会话属主。冒号分隔，不能再走只允许字母数字的 client 正则。"""
    return "kb:" + user_id + ":" + check_tenant(tenant)


def member(user_id: str, tenant: str) -> bool:
    with db.connect() as conn:
        row = conn.execute(
            "SELECT 1 FROM tenant_user WHERE user_id = %s AND tenant_name = %s",
            (user_id, tenant),
        ).fetchone()
    return row is not None


def open_session(owner_id: str, session_id: str | None) -> str:
    memory = ShortTermMemory()
    if session_id:
        if not memory.owns(owner_id, session_id):
            raise KbError("会话不存在", status=404, code="session_not_found")
        return str(session_id)
    return memory.create_session(owner_id)


def history_of(session_id: str) -> str:
    if not config.MEMORY_ENABLED:
        return ""
    return ShortTermMemory().load_context(session_id)


def remember(session_id: str, question: str, answer: str, hits: list[dict]) -> None:
    if not config.MEMORY_ENABLED:
        return
    ShortTermMemory().save_round(session_id, question, answer, {"hits": hits})
    _defer_compress(session_id)


def messages(owner_id: str, session_id: str) -> list[dict]:
    rows = ShortTermMemory().list_messages(owner_id, session_id)
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


def list_sessions(owner_id: str) -> list[dict]:
    """这个属主名下的会话列表，按最后活跃时间倒序。"""
    return ShortTermMemory().list_sessions(owner_id)


def recent_questions(owner_id: str, limit: int = 20) -> list[dict]:
    """这个属主最近问过的原话。点一下是再问一句，不是打开旧会话。"""
    return ShortTermMemory().recent_questions(owner_id, limit=limit)
