"""
llm/middleware.py —— 模型调用中间件 `wrap_call_model`

用 **LangChain 官方的 AgentMiddleware 钩子**（`wrap_model_call` / `awrap_model_call`）
做动态选模型与失败切换，不再自己造 BaseChatModel 子类去写调用循环。

用法：

    from langchain.agents import create_agent
    from llm import wrap_call_model

    agent = create_agent(model=primary_model(), middleware=[wrap_call_model])
    agent.invoke({"messages": [{"role": "user", "content": "..."}]})

中间件在模型调用外再包一层：按模型池顺序取候选，某模型额度用尽 / 限流 / 抖动，
就把 `request.override(model=<下一个>)` 交给 handler 重试；401/400 这类换模型也没用的错误直接抛。
"""
from __future__ import annotations

import logging
from typing import Any, Callable

from langchain.agents.middleware import AgentMiddleware, ModelRequest, ModelResponse
from langchain_core.messages import AIMessage

import config
from llm.pool import FailureKind, ModelSpec, classify_error
from llm.router import (
    LLMUnavailableError,
    _no_candidate_error,
    _status_brief,
    chat_model,
    fallback_spec,
    get_router,
    in_pool,
)

log = logging.getLogger("llm.middleware")


# ============================================================
# 小工具（与具体中间件机制无关）
# ============================================================
def _first_message(resp: Any) -> AIMessage | None:
    """从 ModelResponse / AIMessage / ExtendedModelResponse 里取出第一条消息。"""
    if isinstance(resp, AIMessage):
        return resp
    result = getattr(resp, "result", None)
    if result is None:
        inner = getattr(resp, "model_response", None)
        result = getattr(inner, "result", None)
    if isinstance(result, list) and result:
        first = result[0]
        return first if isinstance(first, AIMessage) else None
    return None


def _usage(msg: AIMessage | None) -> tuple[int, int]:
    um = getattr(msg, "usage_metadata", None) or {}
    pin, pout = int(um.get("input_tokens") or 0), int(um.get("output_tokens") or 0)
    if not pin and not pout:
        tu = (getattr(msg, "response_metadata", None) or {}).get("token_usage") or {}
        pin = int(tu.get("prompt_tokens") or 0)
        pout = int(tu.get("completion_tokens") or 0)
    return pin, pout


def _starved(msg: AIMessage | None) -> str:
    """推理模型把 completion 预算花在 reasoning 上 → content 为空。

    返回非空串表示「该加预算重试」，不是模型故障。
    """
    if msg is None:
        return ""
    content = msg.content
    if not isinstance(content, str) or content.strip():
        return ""
    meta = getattr(msg, "response_metadata", None) or {}
    reasoning = (getattr(msg, "additional_kwargs", None) or {}).get("reasoning_content")
    finish = meta.get("finish_reason")
    if finish == "length" or reasoning:
        return f"finish_reason={finish}, reasoning={'有' if reasoning else '无'}"
    return ""


def _model_params(request: ModelRequest) -> dict[str, Any]:
    """继承调用方（Agent 主模型）的采样参数，只是换 model。"""
    base = request.model
    return {
        "temperature": getattr(base, "temperature", None),
        "max_tokens": getattr(base, "max_tokens", None),
    }


class _Plan:
    """一次模型调用的候选序列 + 失败记账，供同步/异步两个钩子共用。

    顺序：池内可用模型（.env 里 LLM_MODEL_POOL 的顺序）→ 全都不行时用 LLM_MODEL 兜底。
    """

    def __init__(self, request: ModelRequest):
        self.request = request
        self.params = _model_params(request)
        self.specs: list[ModelSpec] = get_router().candidates()
        self.fallback: ModelSpec | None = fallback_spec()
        self.tried: set[str] = set()
        self.errors: list[str] = []
        self.starved: list[str] = []

    def order(self):
        """先池内，后兜底；兜底已在池内（且这轮试过）就不重复试。"""
        for spec in self.specs:
            self.tried.add(spec.name)
            yield spec
        if self.fallback and self.fallback.name not in self.tried:
            self.tried.add(self.fallback.name)
            log.warning("池内模型本轮都不可用，改用兜底模型 %s（LLM_MODEL）", self.fallback.name)
            yield self.fallback

    def model_for(self, spec: ModelSpec, *, max_tokens: int | None = None):
        params = dict(self.params)
        if max_tokens is not None:
            params["max_tokens"] = max_tokens
        return chat_model(spec, **params)

    def budget(self, spec: ModelSpec) -> int:
        return int(self.params.get("max_tokens") or config.LLM_MAX_TOKENS)

    def on_error(self, spec: ModelSpec, exc: BaseException) -> bool:
        """记一次失败。返回 True 表示可以换下一个；False 表示换也没用（需原样抛出）。"""
        kind, reason = classify_error(exc)
        if not kind.switchable:
            return False
        if in_pool(spec.name):
            # 额度用尽会被这里标成 removed（移出池），其它错误进冷却队列
            get_router().mark_failure(spec, kind, reason)
        else:
            log.warning("兜底模型 %s 也失败（%s）：%s", spec.name, kind.value, reason)
        self.errors.append(f"{spec.name}({kind.value})")
        log.warning("模型 %s 不可用（%s），切下一个", spec.name, kind.value)
        return True

    def on_starved(self, spec: ModelSpec, why: str) -> None:
        # 调用方预算不足，不是模型坏了：不动池状态，否则一次小预算请求会连累所有调用
        self.starved.append(spec.name)
        self.errors.append(f"{spec.name}(empty)")
        log.warning("模型 %s %s，切下一个", spec.name, why)

    def on_success(self, spec: ModelSpec, msg: AIMessage | None, resp: Any) -> Any:
        if in_pool(spec.name):
            get_router().mark_success(spec, *_usage(msg))
        if msg is not None and isinstance(getattr(msg, "response_metadata", None), dict):
            msg.response_metadata["pool_model"] = spec.name  # 让 trace 能看出这次是谁答的
        log.info("模型 %s 调用成功 tokens=%d/%d", spec.name, *_usage(msg))
        return resp

    def exhausted(self) -> LLMUnavailableError:
        router = get_router()
        if self.starved and len(self.starved) == len(self.specs):
            return LLMUnavailableError(
                "所有候选模型都只返回了 reasoning 没有正文，判定为 token 预算不足"
                f"（max_tokens={self.params.get('max_tokens')}）。推理模型的 reasoning 会占用"
                f" completion 预算，请调大 LLM_MAX_TOKENS（上限 {config.LLM_MAX_TOKENS_CEILING}）。"
            )
        if not self.specs:
            return _no_candidate_error(router)
        return LLMUnavailableError(
            "模型池本轮全部失败（含兜底模型）："
            + "，".join(self.errors) + "。当前状态：" + _status_brief(router)
        )


# ============================================================
# 中间件
# ============================================================
class WrapCallModelMiddleware(AgentMiddleware):
    """`wrap_model_call` 钩子：动态选模型 + 额度耗尽自动切换。

    同步与异步各一个实现（LangChain 要求两边都声明，否则 ainvoke 会报 NotImplementedError）。
    """

    def wrap_model_call(
        self,
        request: ModelRequest,
        handler: Callable[[ModelRequest], ModelResponse],
    ) -> Any:
        plan = _Plan(request)
        for spec in plan.order():
            budget = plan.budget(spec)
            try:
                resp = handler(request.override(model=plan.model_for(spec)))
                msg = _first_message(resp)
                why = _starved(msg)
                if why and budget < config.LLM_MAX_TOKENS_CEILING:
                    bumped = min(budget * 2, config.LLM_MAX_TOKENS_CEILING)
                    log.info("模型 %s 空回复（%s），max_tokens %d → %d 重试", spec.name, why, budget, bumped)
                    resp = handler(request.override(model=plan.model_for(spec, max_tokens=bumped)))
                    msg, why = _first_message(resp), _starved(_first_message(resp))
                if why:
                    plan.on_starved(spec, why)
                    continue
            except Exception as exc:  # noqa: BLE001
                if not plan.on_error(spec, exc):
                    raise
                continue
            return plan.on_success(spec, msg, resp)

        raise plan.exhausted()

    async def awrap_model_call(
        self,
        request: ModelRequest,
        handler: Callable[[ModelRequest], Any],
    ) -> Any:
        plan = _Plan(request)
        for spec in plan.order():
            budget = plan.budget(spec)
            try:
                resp = await handler(request.override(model=plan.model_for(spec)))
                msg = _first_message(resp)
                why = _starved(msg)
                if why and budget < config.LLM_MAX_TOKENS_CEILING:
                    bumped = min(budget * 2, config.LLM_MAX_TOKENS_CEILING)
                    log.info("模型 %s 空回复（%s），max_tokens %d → %d 重试", spec.name, why, budget, bumped)
                    resp = await handler(request.override(model=plan.model_for(spec, max_tokens=bumped)))
                    msg = _first_message(resp)
                    why = _starved(msg)
                if why:
                    plan.on_starved(spec, why)
                    continue
            except Exception as exc:  # noqa: BLE001
                if not plan.on_error(spec, exc):
                    raise
                continue
            return plan.on_success(spec, msg, resp)

        raise plan.exhausted()


#: 挂到 create_agent(middleware=[wrap_call_model]) 上的实例
wrap_call_model = WrapCallModelMiddleware()
