"""
llm/agent.py —— 把模型池中间件挂到官方 Agent 上

`create_agent(model=..., middleware=[wrap_call_model])` 是 LangChain 1.x 的标准装配方式；
中间件在每次模型调用时按池顺序选模型，额度用尽自动换下一个。
"""
from __future__ import annotations

import logging
import threading

from langchain_core.messages import AIMessage

from llm.middleware import wrap_call_model
from llm.router import primary_model

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


def invoke_model(prompt: str, *, agent=None) -> AIMessage:
    """一次模型调用：走中间件，自动选模型 / 换模型。返回最后一条 AIMessage。"""
    runner = agent or get_model_agent()
    result = runner.invoke({"messages": [{"role": "user", "content": prompt}]})
    messages = result.get("messages") or []
    if not messages:
        raise RuntimeError("Agent 未返回任何消息")
    return messages[-1]
