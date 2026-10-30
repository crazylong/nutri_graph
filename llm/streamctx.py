"""流式调用的线程内挂钩。不放进 RAGState：没声明的键会被丢掉。"""
from __future__ import annotations

from contextlib import contextmanager
from contextvars import ContextVar

_hook: ContextVar[object | None] = ContextVar("chat_stream_hook", default=None)
_nosave: ContextVar[bool] = ContextVar("chat_stream_nosave", default=False)


class StreamCancelled(Exception):
    """客户端断开。不是模型故障，不能记进冷却。"""


class ModelStreamBroken(Exception):
    """已经推出过正文之后模型失败。不换模型，这一轮不落库。"""


def hook():
    return _hook.get()


def skip_save() -> bool:
    return bool(_nosave.get())


def mark_skip_save() -> None:
    _nosave.set(True)


@contextmanager
def bind(target):
    token = _hook.set(target)
    saved = _nosave.set(False)
    try:
        yield target
    finally:
        _hook.reset(token)
        _nosave.reset(saved)
