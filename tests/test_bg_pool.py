"""后台队列满了就丢掉这次任务。"""
from __future__ import annotations

import queue

import utils.spawn as spawn


def test_full_queue_skips(monkeypatch):
    blocked = queue.Queue(maxsize=1)
    blocked.put_nowait(("held", None, None, ()))
    monkeypatch.setattr(spawn, "_queue", lambda: blocked)
    assert spawn.submit_background("skip-me", lambda: None) is False
