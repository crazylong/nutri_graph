"""知识库问答的短链路。食疗图不进这里。"""
from __future__ import annotations

import logging
from typing import TypedDict

from langgraph.graph import END, START, StateGraph

import config
from kb import diversify, neighbor, rerank, store
from kb.common import ABSTAIN, format_hit
from kb.search import search_picture, search_question
from llm import invoke_model, stream_model
from llm.streamctx import hook
from llm.usage import purpose, span

log = logging.getLogger("kb.workflow")


class AskState(TypedDict, total=False):
    tenant_id: str
    question: str
    image: bytes | None
    image_name: str
    mime: str
    hits: list
    history: str
    answer: str
    abstained: bool


def run_ask(*, tenant_id: str, question: str = "", image: bytes | None = None, image_name: str = "", mime: str = "image/png", history: str = "") -> dict:
    label = question.strip() or image_name or "image"
    with span("node", "kb_chat", input=label):
        graph = _graph()
        out = graph.invoke({
            "tenant_id": tenant_id,
            "question": question.strip(),
            "image": image,
            "image_name": image_name,
            "mime": mime,
            "hits": [],
            "history": history,
            "answer": "",
            "abstained": False,
        })
    hits = []
    for hit in out.get("hits") or []:
        item = {
            "chunk_id": hit["chunk_id"],
            "block_kind": hit["block_kind"],
            "text": hit["text"],
            "heading_path": hit["heading_path"],
            "score": hit.get("score"),
        }
        if hit.get("neighbor"):
            item["neighbor"] = True
        if hit.get("image_path") and hit.get("chunk_id"):
            item["image_url"] = f"/kb/images/{hit['chunk_id']}?tenant_id={tenant_id}"
        hits.append(item)
    return {
        "answer": out.get("answer") or ABSTAIN,
        "abstained": bool(out.get("abstained")),
        "hits": hits,
    }


def _retrieve(state: dict) -> dict:
    label = state.get("question") or state.get("image_name") or ""
    with span("node", "kb_retrieve", input=label) as box:
        if not state.get("tenant_id"):
            box.output = "no_tenant"
            return {"hits": []}
        if state.get("image"):
            hits = search_picture(
                state["tenant_id"], state["image"], state.get("mime") or "image/png",
                limit=config.KB_SEARCH_LIMIT,
            )
        else:
            question = state.get("question") or ""
            hits = search_question(
                state["tenant_id"], question, limit=_pool_limit(),
            )
            if _rerank_on():
                hits = _rerank(question, hits)
            hits = _diversify(hits)
            hits = _neighbors(hits)
        box.output = ",".join(hit["chunk_id"] for hit in hits)
    return {"hits": hits}


def _pool_limit() -> int:
    return max(int(config.KB_RERANK_CANDIDATES), int(config.KB_SEARCH_LIMIT))


def _rerank_on() -> bool:
    return bool(
        config.KB_RERANK_ENABLED
        and config.DASHSCOPE_API_KEY
        and config.RERANK_MODEL_POOL
    )


def _rerank(question: str, hits: list[dict]) -> list[dict]:
    with span("node", "kb_rerank", input=question) as box:
        try:
            ranked = rerank.rerank(question, hits)
        except Exception as exc:  # noqa: BLE001
            log.warning("精排异常，退回混合检索顺序：%s", exc)
            ranked = hits
        box.output = ",".join(f"{hit['chunk_id']}:{hit.get('score')}" for hit in ranked)
    return ranked


def _diversify(hits: list[dict]) -> list[dict]:
    limit = config.KB_SEARCH_LIMIT
    if not config.KB_DIVERSIFY_ENABLED:
        return list(hits[:limit])
    label = ",".join(str(hit.get("chunk_id") or "") for hit in hits if isinstance(hit, dict))
    with span("node", "kb_diversify", input=label) as box:
        try:
            kept = diversify.diversify(
                hits,
                limit=limit,
                threshold=config.KB_DIVERSIFY_THRESHOLD,
            )
        except Exception as exc:  # noqa: BLE001
            log.warning("收敛异常，退回排序后的前 N 条：%s", exc)
            kept = list(hits[:limit])
        box.output = ",".join(str(hit.get("chunk_id") or "") for hit in kept if isinstance(hit, dict))
    return kept


def _neighbors(hits: list[dict]) -> list[dict]:
    if not config.KB_NEIGHBOR_ENABLED:
        return hits
    label = ",".join(str(hit.get("chunk_id") or "") for hit in hits if isinstance(hit, dict))
    with span("node", "kb_neighbor", input=label) as box:
        try:
            kept = neighbor.attach(hits, store.neighbors_of(hits))
        except Exception as exc:  # noqa: BLE001
            log.warning("相邻块补齐失败，只保留收敛结果：%s", exc)
            kept = hits
        added = [hit.get("chunk_id") for hit in kept if isinstance(hit, dict) and hit.get("neighbor")]
        box.output = ",".join(str(item) for item in added if item)
    return kept


def _images(state: dict) -> dict:
    with span("node", "kb_image", input="") as box:
        ids = [hit["chunk_id"] for hit in state.get("hits") or [] if hit.get("block_kind") == "image"]
        box.output = ",".join(ids)
    return {}


def _generate(state: dict) -> dict:
    with span("node", "kb_generate", input="") as box:
        hits = state.get("hits") or []
        if not hits:
            box.output = ABSTAIN
            return {"answer": ABSTAIN, "abstained": True}
        question = state.get("question") or "用户上传了一张图，请根据文档块说明它对应的内容。"
        context = "\n\n".join(format_hit(hit) for hit in hits)
        history = (state.get("history") or "").strip()
        history_block = (
            f"近期对话（只用于理解指代，不能当成文档依据）：\n{history}\n\n" if history else ""
        )
        prompt = (
            "你是企业知识库助手。只根据下面的文档块回答，不要补充文档块里没有的内容。"
            f"文档块里没有的内容，直接回答：{ABSTAIN}\n"
            "用 Markdown 写回答：步骤用有序列表，命令和配置用代码块。\n"
            "直接回答问题，只保留必要的命令、配置和结论，不要把文档块原文整段照抄。\n"
            "用到某段依据时，用一句话写明章节路径。\n\n"
            f"{history_block}"
            f"问题：{question}\n\n文档块：\n{context}"
        )
        with purpose("kb_answer"):
            if hook() is not None:
                answer = (stream_model(prompt, enable_thinking=False) or "").strip() or ABSTAIN
            else:
                message = invoke_model(prompt, enable_thinking=False)
                answer = (getattr(message, "content", "") or "").strip() or ABSTAIN
        box.output = answer
    return {"answer": answer, "abstained": False}


def _graph():
    global _compiled
    if _compiled is None:
        wf = StateGraph(AskState)
        wf.add_node("retrieve", _retrieve)
        wf.add_node("images", _images)
        wf.add_node("generate", _generate)
        wf.add_edge(START, "retrieve")
        wf.add_edge("retrieve", "images")
        wf.add_edge("images", "generate")
        wf.add_edge("generate", END)
        _compiled = wf.compile()
    return _compiled


_compiled = None
