"""rid 能进后台线程、日志和 SSE 帧。"""
from __future__ import annotations

import logging

from chatbus import _frame
from llm.usage import current_rid, set_rid
from utils.logging_config import RidFilter
from utils.spawn import spawn


def test_rid_reaches_spawned_thread():
    token = set_rid("abcd1234")
    seen = {}

    def work():
        seen["rid"] = current_rid()

    try:
        spawn("t-rid", work).join(2)
    finally:
        from llm.usage import reset_rid
        reset_rid(token)
    assert seen["rid"] == "abcd1234"


def test_log_filter_defaults_dash():
    # 同进程里别的用例可能留下 rid。这里钉回空值，才是「没有请求上下文」。
    from llm.usage import reset_rid

    token = set_rid("")
    try:
        record = logging.LogRecord("x", logging.INFO, __file__, 1, "hi", (), None)
        assert RidFilter().filter(record) is True
        assert record.rid == "-"
    finally:
        reset_rid(token)


def test_sse_frame_carries_rid():
    token = set_rid("abcd1234")
    try:
        frame = _frame("token", {"text": "hi"})
    finally:
        from llm.usage import reset_rid
        reset_rid(token)
    assert "abcd1234" in frame
