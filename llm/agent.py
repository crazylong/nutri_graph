"""
llm/agent.py —— 把模型池中间件挂到官方 Agent 上

`create_agent(model=..., middleware=[wrap_call_model])` 是 LangChain 1.x 的标准装配方式；
中间件在每次模型调用时按池顺序选模型，额度用尽自动换下一个。
"""
from __future__ import annotations

import logging
import threading

import config
from langchain_core.messages import AIMessage, HumanMessage

from llm.middleware import wrap_call_model
from llm.router import override_enable_thinking, primary_model
from llm.streamctx import ModelStreamBroken, StreamCancelled, hook, mark_skip_save

log = logging.getLogger("llm.agent")

_agent = None
_lock = threading.Lock()


def get_model_agent():
    """单例 Agent：只做「一次模型调用」，负责让中间件生效。

    注意：Agent 的主模型只是占位，实际用哪个由 wrap_call_model 在调用时决定。
    """
    global _agent
    if _agent is None:
        with _lock:
            if _agent is None:
                from langchain.agents import create_agent

                _agent = create_agent(model=primary_model(), middleware=[wrap_call_model])
                log.info("模型池 Agent 就绪（middleware=[wrap_call_model]）")
    return _agent


def invoke_model(prompt: str, *, agent=None, enable_thinking: bool | None = None) -> AIMessage:
    """一次模型调用：走中间件，自动选模型 / 换模型。返回最后一条 AIMessage。

    enable_thinking=False 让混合思考模型直接出正文。问答接地生成用这个，
    否则推理会把 max_tokens 花完，中间件再放大预算重试一次。
    """
    runner = agent or get_model_agent()
    with override_enable_thinking(enable_thinking):
        result = runner.invoke({"messages": [{"role": "user", "content": prompt}]})
    messages = result.get("messages") or []
    if not messages:
        raise RuntimeError("Agent 未返回任何消息")
    return messages[-1]


def _piece(chunk) -> str:
    content = getattr(chunk, "content", "") or ""
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts = []
        for block in content:
            if isinstance(block, str):
                parts.append(block)
            elif isinstance(block, dict) and block.get("type") == "text":
                parts.append(block.get("text") or "")
        return "".join(parts)
    return ""


def _usage_of(meta) -> tuple[int, int]:
    if not meta:
        return 0, 0
    if isinstance(meta, dict):
        return int(meta.get("input_tokens") or 0), int(meta.get("output_tokens") or 0)
    return int(getattr(meta, "input_tokens", 0) or 0), int(getattr(meta, "output_tokens", 0) or 0)


def _read_stream(model, prompt: str, target, seen: list) -> tuple[str, tuple[int, int], bool]:
    """返回 (正文, 最后一个非空用量, 是否因政治类停止转发)。"""
    parts: list[str] = []
    usage = None
    stopped = False
    for chunk in model.stream([HumanMessage(content=prompt)], stream_usage=True):
        if target is not None and target.cancelled():
            mark_skip_save()
            raise StreamCancelled()
        meta = getattr(chunk, "usage_metadata", None)
        if meta:
            usage = meta
        if stopped:
            continue
        piece = _piece(chunk)
        if not piece:
            continue
        parts.append(piece)
        seen.append(piece)
        if target is not None and target.feed(piece) is False:
            stopped = True
    return "".join(parts), _usage_of(usage), stopped


def stream_model(prompt: str, *, enable_thinking: bool | None = False) -> str:
    """按池顺序流式取正文。失败记账与同步中间件同一套。"""
    from llm.middleware import _Plan, params_from

    target = hook()
    plan = _Plan(params_from())
    seen: list[str] = []
    with override_enable_thinking(enable_thinking):
        for spec in plan.order():
            if target is not None and target.cancelled():
                mark_skip_save()
                raise StreamCancelled()
            budget = plan.budget(spec)
            try:
                text, usage, stopped = _read_stream(plan.model_for(spec), prompt, target, seen)
                if not text.strip() and not stopped:
                    bumped = min(max(budget, 1) * 2, config.LLM_MAX_TOKENS_CEILING)
                    log.info(
                        "模型 %s 流式正文为空，max_tokens %s → %s 重试",
                        spec.name, budget, bumped,
                    )
                    text, usage, stopped = _read_stream(
                        plan.model_for(spec, max_tokens=bumped), prompt, target, seen,
                    )
                if text.strip() or stopped:
                    if usage == (0, 0):
                        log.warning("流式响应没有 usage_metadata，本次记 0 token")
                    pin, pout = usage
                    msg = AIMessage(
                        content=text,
                        usage_metadata={
                            "input_tokens": pin,
                            "output_tokens": pout,
                            "total_tokens": pin + pout,
                        },
                    )
                    plan.on_success(spec, msg, msg)
                    return text
                plan.on_starved(spec, "流式正文为空")
            except StreamCancelled:
                raise
            except Exception as exc:  # noqa: BLE001
                if seen:
                    plan.on_error(spec, exc)
                    mark_skip_save()
                    raise ModelStreamBroken(str(exc)) from exc
                if not plan.on_error(spec, exc):
                    raise
        raise plan.exhausted()
