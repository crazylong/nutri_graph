"""
llm —— 大语言模型调用层

一句话：把「用哪个模型」从业务代码里拿走，交给 LangChain 的中间件。

    from llm import invoke_model
    answer = invoke_model(prompt).content          # Agent + wrap_call_model 中间件

模型来自 config.LLM_MODEL_POOL（百炼账号下「模型类型 = 大语言模型」的那几个），
按额度到期日由近到远排序，先用即将过期的。某个模型额度用尽后自动切下一个，
状态落盘在 config.LLM_POOL_STATE_PATH，重启不会再撞同一个已空的模型。

两种官方机制，各管一段：
- `wrap_call_model`：AgentMiddleware 钩子，挂 `create_agent(middleware=[...])`，主链路用。
- `pool_chain()`    ：`head.with_fallbacks(rest)`，给需要原生 BaseChatModel 的场合（离线评估等）。
"""
from llm.pool import (
    FailureKind,
    ModelRouter,
    ModelSpec,
    ModelState,
    ModelStatus,
    classify_error,
)
from llm.middleware import WrapCallModelMiddleware, wrap_call_model
from llm.router import (
    LLMUnavailableError,
    chat_model,
    get_router,
    pool_chain,
    pool_snapshot,
    primary_model,
    probe_model,
    reset_pool_state,
)
from llm.agent import get_model_agent, invoke_model, stream_model

__all__ = [
    "FailureKind",
    "LLMUnavailableError",
    "ModelRouter",
    "ModelSpec",
    "ModelState",
    "ModelStatus",
    "WrapCallModelMiddleware",
    "chat_model",
    "classify_error",
    "get_model_agent",
    "get_router",
    "invoke_model",
    "pool_chain",
    "pool_snapshot",
    "primary_model",
    "probe_model",
    "reset_pool_state",
    "stream_model",
    "wrap_call_model",
]
