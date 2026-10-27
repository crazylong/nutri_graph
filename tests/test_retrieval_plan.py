"""检索契约：不连 Neo4j、不调 LLM。

锁住四类问法各自该查的关系，以及弃答 / 文档降级的分界。
"""
from __future__ import annotations

from starlette.requests import Request

import config
from api.deps import client_ip
from graph.boundary import build_document_prompt, build_grounded_prompt
from graph.nodes import (
    _recent_dialogue,
    generate_node,
    memory_write_node,
    route_after_link,
)
from kg.normalize import EntityLinker, get_alias_store
from kg.query import TEMPLATES, detect_intent, plan_queries


def _req(headers: dict[str, str], client=("10.0.0.8", 1234)) -> Request:
    raw = [(k.lower().encode(), v.encode()) for k, v in headers.items()]
    scope = {
        "type": "http",
        "asgi": {"version": "3.0"},
        "http_version": "1.1",
        "method": "GET",
        "scheme": "http",
        "path": "/",
        "raw_path": b"/",
        "query_string": b"",
        "headers": raw,
        "client": client,
        "server": ("test", 80),
    }
    return Request(scope)


def test_cannot_eat_together_is_incompatible_only():
    assert detect_intent("菠菜豆腐不能同吃") == ["incompatible"]


def test_spinach_tofu_plans_incompatible_edges():
    question = "菠菜和豆腐能一起吃吗"
    intents = detect_intent(question)
    assert "incompatible" in intents
    entities = [
        {"label": "Ingredient", "canonical_id": "ING_SPINACH"},
        {"label": "Ingredient", "canonical_id": "ING_TOFU"},
    ]
    plans = plan_queries(entities, intents)
    incompatible = [p for p in plans if p["intent"] == "incompatible"]
    assert {p["canonical_id"] for p in incompatible} == {"ING_SPINACH", "ING_TOFU"}
    assert "INCOMPATIBLE_WITH" in TEMPLATES["incompatible"]


def test_plain_pairing_question_does_not_open_incompatible():
    """「和什么一起吃好」是在问搭配，不是在问相克。"""
    intents = detect_intent("黑豆和什么一起吃好")
    assert "pair" in intents
    assert "incompatible" not in intents


def test_grapefruit_statin_plans_contra_item():
    intents = detect_intent("西柚影响他汀代谢")
    assert "contra_item" in intents
    entities = [{"label": "Ingredient", "canonical_id": "ING_GRAPEFRUIT"}]
    plans = plan_queries(entities, intents)
    assert any(
        p["intent"] == "contra_item" and p["canonical_id"] == "ING_GRAPEFRUIT"
        for p in plans
    )
    assert "CONTRA_ITEM" in TEMPLATES["contra_item"]


def test_symptom_question_uses_symptom_id_not_ingredient():
    intents = detect_intent("上火了吃什么")
    assert "symptom_remedy" in intents
    plans = plan_queries(
        [{"label": "Symptom", "canonical_id": "SYM_HEAT"}],
        intents,
    )
    assert plans == [{
        "intent": "symptom_remedy",
        "canonical_id": "SYM_HEAT",
        "label": "Symptom",
    }]
    assert "Symptom {canonical_id: $cid}" in TEMPLATES["symptom_remedy"]
    assert plan_queries(
        [{"label": "Ingredient", "canonical_id": "ING_1"}],
        ["symptom_remedy"],
    ) == []


def test_symptom_only_question_goes_to_graph():
    state = {
        "_entities": [{"label": "Symptom", "canonical_id": "SYM_HEAT"}],
        "_intents": ["symptom_remedy"],
        "_unresolved": [],
    }
    assert route_after_link(state) == "graph_query"


def test_nothing_linked_falls_through_to_vector():
    state = {"_entities": [], "_intents": ["pair"], "_unresolved": []}
    assert route_after_link(state) == "vector_fallback"


def test_abstain_when_graph_and_vector_are_both_empty():
    out = generate_node({
        "question": "量子食疗怎么治",
        "_graph_results": [],
        "_vector_results": [],
        "_entities": [],
    })
    assert "确切依据" in out["answer"]
    assert out["confidence"] == 0.0


def test_known_ingredient_without_facts_says_it_is_catalogued():
    out = generate_node({
        "question": "菠菜能飞吗",
        "_graph_results": [],
        "_vector_results": [],
        "_entities": [{"label": "Ingredient", "matched_alias": "菠菜"}],
    })
    assert "已经收录" in out["answer"]
    assert "确切依据" in out["answer"]


def test_vector_hit_is_labeled_as_document_not_graph(monkeypatch):
    monkeypatch.setattr(config, "DASHSCOPE_API_KEY", "")
    prompt = build_document_prompt("随便问问", [{"text": "某文档写了菠菜", "doc_id": "d1"}])
    assert "不是知识图谱" in prompt
    assert "某文档写了菠菜" in prompt

    out = generate_node({
        "question": "随便问问",
        "_graph_results": [],
        "_vector_results": [{"text": "某文档写了菠菜", "doc_id": "d1", "score": 0.9}],
        "_entities": [],
    })
    assert "不是图谱结论" in out["answer"]
    assert "某文档写了菠菜" in out["answer"]
    assert out["_degraded"] is True


def test_prompt_includes_recent_dialogue_as_non_fact():
    prompt = build_grounded_prompt(
        "那能一起吃吗",
        [{"name": "豆腐", "etype": "anecdotal", "elevel": "D", "claim": "不宜同食"}],
        "weak",
        history="user: 上次说了豆腐",
    )
    assert "上次说了豆腐" in prompt
    assert "不能当成图谱事实" in prompt


def test_recent_dialogue_reads_the_given_session(monkeypatch):
    class FakeMemory:
        def load_context(self, session_id, n=None):
            assert session_id == "s1"
            return "user: 你好"

    monkeypatch.setattr(config, "MEMORY_ENABLED", True)
    monkeypatch.setattr("memory.short_term.ShortTermMemory", FakeMemory)
    assert _recent_dialogue({"session_id": "s1"}) == "user: 你好"
    assert _recent_dialogue({}) == ""


def test_memory_write_returns_session_id(monkeypatch):
    class FakeMemory:
        def get_session(self, user_id):
            return "sid-1"

        def save_round(self, session_id, question, answer):
            assert session_id == "sid-1"

    monkeypatch.setattr(config, "MEMORY_ENABLED", True)
    monkeypatch.setattr("memory.short_term.ShortTermMemory", FakeMemory)
    monkeypatch.setattr("graph.nodes._get_llm", lambda: None)
    out = memory_write_node({"question": "q", "answer": "a", "user_id": "u"})
    assert out["session_id"] == "sid-1"


class _Store:
    def exact_lookup(self, *args, **kwargs):
        return []

    def fuzzy_lookup(self, *args, **kwargs):
        return []


def test_vector_link_uses_config_threshold(monkeypatch):
    linker = EntityLinker(
        _Store(),
        vector_search=lambda query, top_k=3: [{
            "canonical_id": "ING_1",
            "label": "Ingredient",
            "score": 0.85,
        }],
    )
    monkeypatch.setattr(config, "ENTITY_LINK_THRESHOLD", 0.9)
    assert linker.link("黑豆").entities == []
    monkeypatch.setattr(config, "ENTITY_LINK_THRESHOLD", 0.8)
    assert linker.link("黑豆").entities[0].canonical_id == "ING_1"


def test_alias_store_is_process_singleton():
    import kg.normalize as normalize

    normalize._alias_store = None
    first = get_alias_store()
    second = get_alias_store()
    assert first is second
    normalize._alias_store = None


def test_client_ip_prefers_x_real_ip_over_forwarded_for():
    request = _req({
        "x-real-ip": "1.2.3.4",
        "x-forwarded-for": "9.9.9.9, 1.2.3.4",
    })
    assert client_ip(request) == "1.2.3.4"


def test_client_ip_without_real_ip_uses_peer():
    request = _req({}, client=("10.1.1.1", 9))
    assert client_ip(request) == "10.1.1.1"
