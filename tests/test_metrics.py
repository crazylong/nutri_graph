"""/metrics 文本和计数。"""
from __future__ import annotations

import api.metrics as metrics


def _agent(hit=True, abstain=False, degraded=False):
    class Agent:
        def invoke(self, state):
            return {
                "answer": "好",
                "session_id": None,
                "_graph_hit": hit,
                "_abstain": abstain,
                "_degraded": degraded,
            }
    return Agent()


def test_metrics_counts_two_chats(client, monkeypatch):
    metrics.reset()
    monkeypatch.setattr("graph.workflow.get_agent", lambda: _agent(hit=True))
    assert client.post("/chat", json={"question": "第一问"}).status_code == 200
    monkeypatch.setattr("graph.workflow.get_agent", lambda: _agent(hit=False, abstain=True))
    assert client.post("/chat", json={"question": "第二问"}).status_code == 200
    body = client.get("/metrics").text
    assert 'nutri_chat_total{status="ok"} 1' in body
    assert 'nutri_chat_total{status="abstain"} 1' in body
    assert "nutri_graph_hit_ratio 0.500000" in body
    assert "nutri_fetch_queue_depth" in body
    assert "nutri_llm_pool_available" in body
    assert "nutri_sms_sent_today" in body
    assert "nutri_nutrient_drift" in body
