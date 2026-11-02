"""知识库流式推送。只按句把模型原文推到页面，不走食疗敏感改写。"""
from __future__ import annotations

import time

from llm.streamctx import StreamCancelled

_ENDS = "。！？；\n"


class KbStreamer:
    def __init__(self, push, cancelled=None, clock=None):
        self._push = push
        self._cancelled = cancelled or (lambda: False)
        self._clock = clock or time.monotonic
        self.pending = ""
        self.sent_text = False
        self.done = False
        self.last_push = self._clock()

    def cancelled(self) -> bool:
        return bool(self._cancelled())

    def _emit(self, event: str, data: dict | None = None) -> bool:
        if self.cancelled() and event not in ("error", "done"):
            raise StreamCancelled()
        return bool(self._push(event, data or {}))

    def status(self, text: str) -> None:
        self._emit("status", {"text": text})

    def text_delta(self, text: str) -> None:
        if not text:
            return
        if self._emit("text_delta", {"text": text}):
            self.sent_text = True

    def error(self, text: str) -> None:
        self._emit("error", {"text": text})

    def result(self, body: dict) -> None:
        self._emit("result", body)

    def finish_stream(self) -> None:
        if self._emit("done", {}):
            self.done = True

    def feed(self, piece: str) -> bool:
        if not piece:
            return True
        if self.cancelled():
            raise StreamCancelled()
        self.pending += piece
        now = self._clock()
        boundary = any(mark in self.pending for mark in _ENDS)
        if boundary or len(self.pending) >= 80 or (now - self.last_push) >= 2:
            self.text_delta(self.pending)
            self.pending = ""
            self.last_push = now
        return True

    def flush(self) -> None:
        if not self.pending:
            return
        self.text_delta(self.pending)
        self.pending = ""
