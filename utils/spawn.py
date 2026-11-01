"""后台线程。Thread 不继承 contextvars，这里拷贝一份再跑。"""
from __future__ import annotations

import contextvars
import logging
import queue
import threading

import config

log = logging.getLogger("utils.spawn")

_bg_queue: queue.Queue | None = None
_bg_started = False
_bg_lock = threading.Lock()


def spawn(name: str, target, *args) -> threading.Thread:
    ctx = contextvars.copy_context()
    thread = threading.Thread(
        target=lambda: ctx.run(target, *args),
        name=name,
        daemon=True,
    )
    thread.start()
    return thread


def _worker() -> None:
    assert _bg_queue is not None
    while True:
        name, ctx, target, args = _bg_queue.get()
        try:
            ctx.run(target, *args)
        except Exception as exc:  # noqa: BLE001 —— 后台任务失败不能弄死工人线程
            log.warning("后台任务 %s 失败：%s", name, exc)


def _queue() -> queue.Queue:
    global _bg_queue, _bg_started
    with _bg_lock:
        if _bg_queue is None:
            _bg_queue = queue.Queue(maxsize=max(int(config.BG_WORKERS), 1))
        if not _bg_started:
            for i in range(max(int(config.BG_WORKERS), 1)):
                threading.Thread(target=_worker, name=f"bg-{i}", daemon=True).start()
            _bg_started = True
        return _bg_queue


def submit_background(name: str, target, *args) -> bool:
    """有界队列。满了就丢掉这次任务并打日志，调用方继续。"""
    ctx = contextvars.copy_context()
    try:
        _queue().put_nowait((name, ctx, target, args))
    except queue.Full:
        log.warning("后台队列已满，跳过 %s", name)
        return False
    return True
