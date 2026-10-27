"""
llm/router.py —— 模型实例工厂

这一层只做「把池里的槽位变成 LangChain 能用的模型实例」，
选模型/换模型的动作交给 LangChain 自己的机制：

- Agent 路径：`llm/middleware.py` 的 `wrap_call_model`（官方 AgentMiddleware 钩子）
- 单发路径：`pool_chain()`，即 LangChain 原生的 `Runnable.with_fallbacks`

不再自己造 BaseChatModel 子类去写重试循环——那是 LangChain 已经提供的能力。
"""
from __future__ import annotations

import logging
import threading
from contextlib import contextmanager
from contextvars import ContextVar
from typing import Any

import config
from llm.pool import ModelRouter, ModelSpec, classify_error

log = logging.getLogger("llm.router")


class LLMUnavailableError(RuntimeError):
    """池内模型全部不可用（额度用尽 / 限流 / 全部过期）。调用方应降级而不是崩。"""


_router: ModelRouter | None = None
_lock = threading.Lock()
_models: dict[tuple, Any] = {}   # (模型名, temperature, max_tokens, extras) -> ChatOpenAI
_chains: dict[tuple, Any] = {}   # (候选, temperature, max_tokens, thinking) -> Runnable

# 一次 invoke_model 是否显式开关思考。None = 不传这个参数，沿用模型默认。
_enable_thinking: ContextVar[bool | None] = ContextVar("llm_enable_thinking", default=None)

# 百炼「仅思考」型号关不掉。给它们传 enable_thinking=false 时，
# 有的端点回 400；400 在中间件里算客户端错误，不会换下一个模型。
_THINKING_ONLY = ("2.4t-a95b", "deepseek-r1")


def thinking_can_be_disabled(model_name: str) -> bool:
    name = (model_name or "").lower()
    return not any(mark in name for mark in _THINKING_ONLY)


def current_enable_thinking() -> bool | None:
    return _enable_thinking.get()


@contextmanager
def override_enable_thinking(value: bool | None):
    token = _enable_thinking.set(value)
    try:
        yield
    finally:
        _enable_thinking.reset(token)


def _freeze(value: Any) -> Any:
    """缓存键要可哈希。extra_body 是 dict，不能直接放进 tuple。"""
    if isinstance(value, dict):
        return tuple(sorted((str(k), _freeze(v)) for k, v in value.items()))
    if isinstance(value, (list, tuple)):
        return tuple(_freeze(v) for v in value)
    return value


# ============================================================
# 池
# ============================================================
def get_router() -> ModelRouter:
    global _router
    if _router is None:
        with _lock:
            if _router is None:
                specs = [ModelSpec.parse(m) for m in config.LLM_MODEL_POOL]
                _router = ModelRouter(specs, state_path=config.LLM_POOL_STATE_PATH)
                if specs:
                    log.info("模型池就绪（.env LLM_MODEL_POOL）：%s", " → ".join(s.name for s in specs))
                else:
                    log.warning(
                        "LLM_MODEL_POOL 为空：请在 .env 里配置模型集合，"
                        "例如 LLM_MODEL_POOL=deepseek-v4-flash-0731,qwen3.8-max"
                    )
    return _router


def pool_snapshot() -> dict:
    """池状态 + 兜底模型，给 CLI / GET /llm/pool 用。"""
    snap = get_router().snapshot()
    fb = fallback_spec()
    snap["fallback"] = fb.name if fb else None
    snap["fallback_in_pool"] = bool(fb and get_router().spec(fb.name) is not None)
    return snap


def reset_pool_state() -> None:
    get_router().reset()


# ============================================================
# 实例
# ============================================================
def chat_model(
    spec: ModelSpec,
    *,
    temperature: float | None = None,
    max_tokens: int | None = None,
    enable_thinking: bool | None = None,
    **kwargs,
):
    """把池里的一个模型构造成 ChatOpenAI（走百炼 OpenAI 兼容端点）。

    max_retries=0：同一模型原地重试没有意义，换模型才是有效动作，
    而「换模型」由 wrap_call_model / with_fallbacks 负责。

    enable_thinking=False 关掉混合思考模型的推理（百炼扩展参数，走 extra_body）。
    仅思考型号传了会 400，这里直接省略。
    """
    from langchain_openai import ChatOpenAI

    t = config.LLM_TEMPERATURE if temperature is None else temperature
    mt = config.LLM_MAX_TOKENS if max_tokens is None else max_tokens
    extras = dict(kwargs)
    # 仅思考型号省略这个参数：传 false 会 400，而 400 不会触发换模型。
    if enable_thinking is not None and thinking_can_be_disabled(spec.name):
        body = dict(extras.pop("extra_body", None) or {})
        body["enable_thinking"] = enable_thinking
        extras["extra_body"] = body
    key = (spec.name, t, mt, tuple(sorted((k, _freeze(v)) for k, v in extras.items())))
    model = _models.get(key)
    if model is None:
        model = ChatOpenAI(
            model=spec.name,
            api_key=config.DASHSCOPE_API_KEY,
            base_url=config.DASHSCOPE_BASE_URL,
            temperature=t,
            max_tokens=mt,
            timeout=config.LLM_TIMEOUT,
            max_retries=0,
            **extras,
        )
        _models[key] = model
    return model


def fallback_spec() -> ModelSpec | None:
    """兜底模型（.env 的 LLM_MODEL）：池里全部不可用时才会用到。"""
    name = (config.LLM_MODEL or "").strip()
    return ModelSpec.parse(name) if name else None


def in_pool(name: str) -> bool:
    return get_router().spec(name) is not None


def _no_candidate_error(router: ModelRouter) -> LLMUnavailableError:
    """区分「池子根本没配」和「配了但都不可用」——前者是配置问题，后者看兜底。"""
    if not router.specs:
        return LLMUnavailableError(
            "模型池为空：请在 .env 里配置 LLM_MODEL_POOL（逗号分隔的大语言模型名），"
            "例如 LLM_MODEL_POOL=deepseek-v4-flash-0731,qwen3.8-max"
        )
    return LLMUnavailableError(
        "池内模型当前全部不可用，且兜底模型 LLM_MODEL 也没能救场：" + _status_brief(router)
    )


def primary_model(**kwargs):
    """当前队首模型；池里全不可用时退到兜底模型。"""
    router = get_router()
    cands = router.candidates()
    if cands:
        return chat_model(cands[0], **kwargs)
    fb = fallback_spec()
    if fb is None:
        raise _no_candidate_error(router)
    log.warning("池内模型全部不可用，改用兜底模型 %s", fb.name)
    return chat_model(fb, **kwargs)


def pool_chain(
    *,
    temperature: float | None = None,
    max_tokens: int | None = None,
    enable_thinking: bool | None = None,
):
    """LangChain 原生回退链：`head.with_fallbacks(rest)`。

    给需要原生 BaseChatModel 语义、又不方便进 Agent 的调用点使用
    （画像抽取、离线评估等）。候选集合变了自动重建，已摘掉的模型不会留在链上。
    池里全不可用时退化为兜底模型单链。
    """
    router = get_router()
    cands = router.candidates()
    if not cands:
        fb = fallback_spec()
        if fb is None:
            raise _no_candidate_error(router)
        log.warning("池内模型全部不可用，回退链只用兜底模型 %s", fb.name)
        cands = [fb]
    key = (tuple(s.name for s in cands), temperature, max_tokens, enable_thinking)
    chain = _chains.get(key)
    if chain is None:
        models = [
            chat_model(
                s,
                temperature=temperature,
                max_tokens=max_tokens,
                enable_thinking=enable_thinking,
            )
            for s in cands
        ]
        head, *rest = models
        chain = head.with_fallbacks(rest) if rest else head
        _chains[key] = chain
        log.info("回退链重建：%s", " → ".join(s.name for s in cands))
    return chain


def _status_brief(router: ModelRouter) -> str:
    return "；".join(
        f"{m['name']}={m['status']}" + (f"({m['reason'][:40]})" if m["reason"] else "")
        for m in router.snapshot()["models"]
    )


def probe_model(
    name: str,
    *,
    prompt: str = "只回复 OK",
    max_tokens: int = 16,
    mark: bool = True,
):
    """单独探一个模型（scripts/check_llm_pool --probe 用）。返回 (ok, 信息)。"""
    router = get_router()
    spec = router.spec(name)
    if spec is None:
        return False, "不在 LLM_MODEL_POOL 里"
    try:
        resp = chat_model(spec, max_tokens=max_tokens).invoke(prompt)
    except Exception as exc:  # noqa: BLE001
        kind, reason = classify_error(exc)
        if mark:
            router.mark_failure(spec, kind, reason)
        return False, f"{kind.value}: {reason}"
    if mark:
        router.mark_success(spec, *_usage(resp))
    text = (getattr(resp, "content", "") or "").strip().replace("\n", " ")
    return True, f"ok  tokens={_usage(resp)[0]}/{_usage(resp)[1]}  content={text[:30]!r}"


def _usage(resp: Any) -> tuple[int, int]:
    um = getattr(resp, "usage_metadata", None) or {}
    pin, pout = int(um.get("input_tokens") or 0), int(um.get("output_tokens") or 0)
    if not pin and not pout:
        tu = (getattr(resp, "response_metadata", None) or {}).get("token_usage") or {}
        pin = int(tu.get("prompt_tokens") or 0)
        pout = int(tu.get("completion_tokens") or 0)
    return pin, pout
