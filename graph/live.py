"""按句把模型正文推到页面上。比较用的已推文字不含免责前缀。"""
from __future__ import annotations

import logging
import time

from kg.sensitive import MEDICAL_FALLBACK, POLITICAL_ANSWER, rewrite_answer
from llm.streamctx import StreamCancelled

log = logging.getLogger("graph.live")

_ENDS = "。！？；\n"


class Live:
    def __init__(self, push, cancelled=None, clock=None):
        self._push = push
        self._cancelled = cancelled or (lambda: False)
        self._clock = clock or time.monotonic
        self.hold = ""
        self.hold_nl = False
        self.pushed = ""
        self.raw = ""
        self.pending = ""
        self.screen = ""
        self.stopped = False
        self.checked = False
        self.broken = False
        self.done = False
        self.sent_text = False
        self.ever_ok = False
        self.last_push = self._clock()

    def cancelled(self) -> bool:
        return bool(self._cancelled())

    def emit(self, event: str, data: dict | None = None) -> bool:
        if self.broken:
            return False
        if self.cancelled() and event not in ("error", "done"):
            raise StreamCancelled()
        ok = self._push(event, data or {})
        if not ok:
            self.broken = True
            return False
        self.ever_ok = True
        if event == "done":
            self.done = True
        if event == "text_delta":
            self.sent_text = True
            self.screen += data.get("text") or ""
        elif event == "answer_replace":
            self.sent_text = True
            self.screen = "" if data is None else data.get("text", "")
        return True

    def status(self, text: str) -> None:
        self.emit("status", {"text": text})

    def text_delta(self, text: str) -> None:
        if text:
            self.emit("text_delta", {"text": text})

    def answer_replace(self, text: str) -> None:
        self.emit("answer_replace", {"text": text})

    def error(self, text: str) -> None:
        self.emit("error", {"text": text})

    def result(self, body: dict) -> None:
        self.emit("result", body)

    def finish_stream(self) -> None:
        self.emit("done", {})

    def push_note(self, note: str) -> None:
        self.hold = note
        self.hold_nl = True
        self.text_delta(note + "\n")

    def push_fixed(self, text: str) -> None:
        self.hold = text
        self.hold_nl = False
        self.text_delta(text)

    def align(self, text: str) -> None:
        if self.screen != text:
            self.answer_replace(text)

    def visible(self, body: str) -> str:
        if body == POLITICAL_ANSWER:
            return POLITICAL_ANSWER
        if not body or body == MEDICAL_FALLBACK:
            return self.hold
        if not self.hold:
            return body
        if self.hold_nl:
            return self.hold + "\n" + body
        return self.hold + body

    def feed(self, piece: str) -> bool:
        if self.stopped or not piece:
            return not self.stopped
        if self.cancelled():
            raise StreamCancelled()
        self.raw += piece
        self.pending += piece
        now = self._clock()
        boundary = any(mark in self.pending for mark in _ENDS)
        if boundary or len(self.pending) >= 80 or (now - self.last_push) >= 2:
            self._apply(self.raw)
            self.pending = ""
            self.last_push = now
        return not self.stopped

    def finish(self) -> str:
        if self.raw:
            self._apply(self.raw)
            self.pending = ""
        final = self._final()
        self.align(final)
        return final

    def _final(self) -> str:
        if self.stopped:
            return POLITICAL_ANSWER
        body = self._rewrite(self.raw) if self.raw else ""
        if body == POLITICAL_ANSWER:
            return POLITICAL_ANSWER
        if not body or body == MEDICAL_FALLBACK:
            if self.hold and self.hold_nl:
                return self.hold
            if not self.hold:
                return MEDICAL_FALLBACK
            return self.hold
        return self.visible(body)

    def _rewrite(self, text: str) -> str:
        try:
            cleaned, _hits = rewrite_answer(text)
        except Exception as exc:  # noqa: BLE001
            log.warning("输出扫描失败，原样返回：%s", exc)
            return text
        return cleaned

    def _apply(self, accumulated: str) -> None:
        raw = self._rewrite(accumulated)
        compared = raw.rstrip()
        pushed = self.pushed.rstrip()
        if compared == POLITICAL_ANSWER:
            self.answer_replace(POLITICAL_ANSWER)
            self.pushed = POLITICAL_ANSWER
            self.stopped = True
            return
        if compared == MEDICAL_FALLBACK:
            if self.pushed:
                self.answer_replace(self.hold)
            elif not self.checked:
                self.status("正在核对表述")
                self.checked = True
            self.pushed = ""
            return
        if compared == pushed:
            return
        if compared.startswith(pushed):
            if raw.startswith(self.pushed):
                delta = raw[len(self.pushed):]
            elif raw.startswith(pushed):
                delta = raw[len(pushed):]
            else:
                delta = compared[len(pushed):]
            if delta:
                self.text_delta(delta)
            self.pushed = raw
            return
        self.answer_replace(self.visible(compared))
        self.pushed = raw
