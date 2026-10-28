"""检索契约：不连 Neo4j、不调 LLM。

锁住四类问法各自该查的关系，以及弃答 / 文档降级的分界。
"""
from __future__ import annotations

from starlette.requests import Request

import config
from api.deps import client_ip
from graph.boundary import (
    build_document_prompt,
    build_grounded_prompt,
    mentions_health_constraint,
    normalize_profile_fields,
)
from graph.nodes import (
    _complete_from_prompt,
    _recent_dialogue,
    generate_node,
    memory_write_node,
    route_after_link,
    route_node,
)
from llm.router import thinking_can_be_disabled
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


def test_prompt_uses_age_and_symptoms_without_inventing():
    profile = {
        "age": 35,
        "gender": "女",
        "symptoms": ["失眠"],
        "hobbies": ["跑步"],
        "allergies": ["花生"],
    }
    prompt = build_grounded_prompt(
        "吃什么",
        [{"name": "豆腐", "etype": "guide", "elevel": "A", "claim": "含钙"}],
        "strong",
        profile=profile,
    )
    assert "35岁" in prompt
    assert "失眠" in prompt
    assert "花生" in prompt
    assert "跑步" not in prompt
    assert "不能用来编造" in prompt
    doc = build_document_prompt("吃什么", [{"text": "文档", "doc_id": "d"}], profile)
    assert "失眠" in doc and "跑步" not in doc


def test_jin_converts_to_kg_and_empty_fields_drop():
    out = normalize_profile_fields({
        "weight_jin": 120,
        "weight_kg": None,
        "allergies": [],
        "conditions": ["痛风", "生理期"],
        "symptoms": ["失眠"],
        "gender": "女性",
        "age": "35",
        "height_cm": 1.65,
        "hobbies": ["跑步", ""],
    })
    assert out["weight_kg"] == 60.0
    assert "allergies" not in out
    assert out["conditions"] == ["痛风"]
    assert out["symptoms"] == ["失眠", "生理期"]
    assert out["gender"] == "女"
    assert out["age"] == 35
    assert out["height_cm"] == 165.0
    assert out["hobbies"] == ["跑步"]
    assert normalize_profile_fields({"gender": "我喜欢吃番茄", "age": None}) == {}
    assert normalize_profile_fields({"weight_kg": 60, "weight_jin": 120})["weight_kg"] == 60.0


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
        def create_session(self, user_id):
            assert user_id == "u"
            return "sid-1"

        def save_round(self, session_id, question, answer, card=None):
            assert session_id == "sid-1"
            assert card["graph_hit"] is False

    monkeypatch.setattr(config, "MEMORY_ENABLED", True)
    monkeypatch.setattr("memory.short_term.ShortTermMemory", FakeMemory)
    out = memory_write_node({"question": "q", "answer": "a", "user_id": "u"})
    assert out["session_id"] == "sid-1"


def test_memory_write_keeps_explicit_session(monkeypatch):
    class FakeMemory:
        def create_session(self, user_id):
            raise AssertionError("已有会话不应再新建")

        def save_round(self, session_id, question, answer, card=None):
            assert session_id == "keep"

    monkeypatch.setattr(config, "MEMORY_ENABLED", True)
    monkeypatch.setattr("memory.short_term.ShortTermMemory", FakeMemory)
    out = memory_write_node({
        "question": "q", "answer": "a", "user_id": "u", "session_id": "keep",
    })
    assert out["session_id"] == "keep"


def test_health_constraint_cues():
    assert mentions_health_constraint("我对花生过敏")
    assert mentions_health_constraint("痛风能吃海鲜吗")
    assert mentions_health_constraint("我今年35岁")
    assert mentions_health_constraint("我120斤")
    assert mentions_health_constraint("我怀孕了")
    assert not mentions_health_constraint("黑豆和什么一起吃")
    assert not mentions_health_constraint("男人补锌")
    assert not mentions_health_constraint("我喜欢吃番茄")


def test_memory_write_defers_profile_only_when_cued(monkeypatch):
    class FakeMemory:
        def create_session(self, user_id):
            return "sid-1"

        def save_round(self, session_id, question, answer, card=None):
            return None

    deferred = []
    monkeypatch.setattr(config, "MEMORY_ENABLED", True)
    monkeypatch.setattr(config, "DASHSCOPE_API_KEY", "test-key")
    monkeypatch.setattr("memory.short_term.ShortTermMemory", FakeMemory)
    monkeypatch.setattr("graph.nodes._defer_profile", lambda *args: deferred.append(args))

    memory_write_node({"question": "黑豆怎么吃", "answer": "a", "user_id": "u"})
    assert deferred == []

    memory_write_node({"question": "我对花生过敏", "answer": "先别吃", "user_id": "u"})
    assert deferred == [("u", "我对花生过敏", "先别吃")]

    memory_write_node({
        "question": "我对花生过敏",
        "answer": "先别吃",
        "user_id": "u",
        "consent_profile": False,
    })
    assert deferred == [("u", "我对花生过敏", "先别吃")]


def test_denied_consent_does_not_load_profile(monkeypatch):
    def boom(*_args, **_kwargs):
        raise AssertionError("拒绝收集时不应读画像")

    monkeypatch.setattr("memory.long_term.UserProfile.get", boom)
    out = route_node({
        "question": "黑豆怎么吃",
        "user_id": "u",
        "consent_profile": False,
    })
    assert out["_user_profile"] == {}
    assert out["_intents"]


def test_answer_call_disables_thinking(monkeypatch):
    seen = {}

    class Msg:
        content = '{"answer":"行","basis":[],"gaps":[],"confidence":0.8}'

    def fake(prompt, **kwargs):
        seen.update(kwargs)
        return Msg()

    monkeypatch.setattr("llm.invoke_model", fake)
    out = _complete_from_prompt("p")
    assert seen["enable_thinking"] is False
    assert out["answer"] == "行"


def test_thinking_only_models_skip_the_flag():
    assert thinking_can_be_disabled("qwen3.8-max")
    assert thinking_can_be_disabled("deepseek-v4-flash-0731")
    assert not thinking_can_be_disabled("qwen3.8-2.4t-a95b")
    assert not thinking_can_be_disabled("deepseek-r1")


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
