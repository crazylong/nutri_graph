"""SSE 槽位拿不到就立刻 503，不把线程堆起来。"""
from __future__ import annotations

import threading

from chatbus import MemoryBus


def test_stream_busy(client, monkeypatch):
    monkeypatch.setattr("chatbus.open_bus", lambda: MemoryBus())
    monkeypatch.setattr("api.chat._slots", lambda: threading.Semaphore(0))

    class Agent:
        def invoke(self, state):
            return {"answer": "不该跑到", "session_id": None}

    monkeypatch.setattr("graph.workflow.get_agent", lambda: Agent())
    response = client.post("/chat/stream", json={"question": "忙"})
    assert response.status_code == 503
    assert response.json()["detail"]["code"] == "stream_busy"
