"""未收录食材的当轮网页参考。搜索和生成都换成假的，不打外部接口。"""
from __future__ import annotations

from pathlib import Path

import pytest

import config
from graph.boundary import build_web_prompt, mentions_health_constraint
from graph.nodes import _eligible_terms, route_after_graph, route_after_link
from kg.request import extract_candidates
from kg.sensitive import MEDICAL_FALLBACK, POLITICAL_ANSWER, term_hash
from kg.webanswer import _clamp, _filter, answer, cache_key
from kg.webguard import is_herbal, trim_sentences
from kg.websearch import WebResult, WebSearchError, search
from tests.helpers import requires_pg

SAMPLE = "猪脚跟天麻煲汤合适吗"
COOKING = "天麻炖猪脚是家常做法，先焯水再下锅，汤色才会清，这是家常步骤，火候中等即可，盐少放一点。"


def _row(content: str, title: str = "家常汤谱", url: str = "https://cook.example.com/tang") -> WebResult:
    return WebResult(title=title, content=content, url=url, site="cook.example.com")


def _state(question: str, unresolved: list[str] | None = None, user: str = "web-v4") -> dict:
    return {
        "question": question,
        "user_id": user,
        "_unresolved": unresolved if unresolved is not None else extract_candidates(question),
        "_intents": ["pair"],
        "_entities": [],
        "_near_misses": [],
    }


@pytest.fixture(autouse=True)
def _web_defaults(monkeypatch):
    monkeypatch.setattr(config, "WEB_SEARCH_ENABLED", True)
    monkeypatch.setattr(config, "WEB_SEARCH_PROVIDER", "zhipu_sogou")
    monkeypatch.setattr(config, "WEB_SEARCH_API_KEY", "test-key")
    monkeypatch.setattr(config, "WEB_ANSWER_DAILY_BUDGET", 100000)
    monkeypatch.setattr(config, "WEB_ANSWER_USER_HOURLY", 1000)
    monkeypatch.setattr(config, "WEB_ANSWER_PROMPT_VERSION", "v1")
    monkeypatch.setattr(config, "SENSITIVE_GUARD_ENABLED", True)
    monkeypatch.setattr("graph.nodes._entity_vector_search", lambda *args, **kwargs: [])
    import graph.workflow as workflow

    workflow._graph = None
    yield
    workflow._graph = None


@pytest.fixture
def calls():
    box = {"search": [], "generate": []}

    def fake_search(query: str):
        box["search"].append(query)
        return [_row(COOKING)]

    def fake_generate(prompt: str):
        box["generate"].append(prompt)
        return {"answer": "天麻炖猪脚是家常做法。", "cited": ["cook.example.com"], "confidence": 0.9}

    return box, fake_search, fake_generate


def _purge(question: str, user_id: str | None = None) -> None:
    import db

    key = cache_key(question)
    with db.connect() as conn:
        conn.execute("DELETE FROM web_answer_cache WHERE query_hash = %s", (key,))
        conn.execute("DELETE FROM web_search_log WHERE query_hash = %s", (key,))
        if user_id:
            conn.execute(
                "DELETE FROM kg_fetch_task WHERE request_id IN "
                "(SELECT id FROM kg_ingredient_request WHERE requested_by = %s)",
                (user_id,),
            )
            conn.execute(
                "DELETE FROM kg_ingredient_request WHERE requested_by = %s",
                (user_id,),
            )
            conn.execute("DELETE FROM web_search_log WHERE user_id = %s", (user_id,))


def _cache_count(question: str) -> int:
    import db

    with db.connect() as conn:
        row = conn.execute(
            "SELECT count(*) FROM web_answer_cache WHERE query_hash = %s",
            (cache_key(question),),
        ).fetchone()
    return int(row[0])


def _log_count(question: str) -> int:
    import db

    with db.connect() as conn:
        row = conn.execute(
            "SELECT count(*) FROM web_search_log WHERE query_hash = %s",
            (cache_key(question),),
        ).fetchone()
    return int(row[0])


# ---------------------------------------------------------------
# 不连库也能定的规则
# ---------------------------------------------------------------
def test_break_words_split_the_sample_sentence():
    assert extract_candidates(SAMPLE) == ["猪脚", "天麻"]
    assert extract_candidates("猪脚、天麻、当归煲汤合适吗") == ["猪脚", "天麻", "当归"]
    # 没有顿号时 6 个字并成一个键，所以断言 12 必须带顿号
    assert extract_candidates("猪脚天麻当归煲汤合适吗") == ["猪脚天麻当归"]
    # 已知副作用：煲 会把煲仔饭切成仔饭
    assert extract_candidates("煲仔饭怎么做") == ["仔饭"]


def test_self_report_filter_keeps_weight_words():
    pregnant = "我怀孕了，猪脚和天麻能一起煲汤吗"
    assert extract_candidates(pregnant) == ["我怀孕了", "猪脚", "天麻"]
    assert _eligible_terms(_state(pregnant)) == ["猪脚", "天麻"]
    assert _eligible_terms(_state("二斤猪肉怎么炖")) == ["二斤猪肉"]
    gout = "我痛风能吃猪脚吗"
    assert "我痛风" in extract_candidates(gout)
    assert _eligible_terms(_state(gout)) == ["猪脚"]


@pytest.mark.no_db
def test_route_searches_when_any_food_is_missing(monkeypatch):
    pregnant = "我怀孕了，猪脚和天麻能一起煲汤吗"
    assert mentions_health_constraint(pregnant)
    assert route_after_link(_state(pregnant)) == "uncatalogued"

    gout = _state("我痛风能吃猪脚吗", unresolved=["我痛风", "猪脚"])
    gout["_intents"] = ["contra_population"]
    assert route_after_link(gout) == "uncatalogued"

    half = _state("牛肉和天麻能一起吃吗", unresolved=["天麻"])
    half["_entities"] = [{"label": "Ingredient", "canonical_id": "ING_BEEF"}]
    half["_intents"] = ["pair"]
    assert route_after_link(half) == "web_answer"

    soup = _state("天麻猪脚莲子汤搭配吗", unresolved=["天麻"])
    soup["_entities"] = [
        {"label": "Ingredient", "canonical_id": "ING_PIG", "matched_alias": "猪脚"},
        {"label": "Ingredient", "canonical_id": "ING_LOTUS", "matched_alias": "莲子"},
    ]
    soup["_intents"] = ["pair"]
    assert route_after_link(soup) == "web_answer"

    noise = _state("牛肉的营养怎么样", unresolved=["营养"])
    noise["_entities"] = [{"label": "Ingredient", "canonical_id": "ING_BEEF"}]
    noise["_intents"] = ["pair"]
    assert route_after_link(noise) == "graph_query"

    monkeypatch.setattr(config, "WEB_SEARCH_ENABLED", False)
    assert route_after_link(half) == "graph_query"
    assert route_after_link(_state(SAMPLE)) == "uncatalogued"


@pytest.mark.no_db
def test_zero_pair_edges_go_to_web_search(monkeypatch):
    from kg.webanswer import _fallback, _lead

    state = {
        "question": "猪脚莲子煲汤可以吗",
        "_graph_hit": False,
        "_web_reason": "pair_gap",
        "_intents": ["pair"],
        "_entities": [
            {"label": "Ingredient", "matched_alias": "莲子"},
            {"label": "Ingredient", "matched_alias": "猪脚"},
        ],
    }
    assert route_after_graph(state) == "web_answer"
    assert "还没进知识库" not in _lead(state, [])
    assert "莲子" in _lead(state, []) and "猪脚" in _lead(state, [])
    failed = _fallback([], state=state, web={"used": False, "sources": [], "blocked": "error"})
    assert "已经收录" in failed["answer"]
    assert "没能对应" not in failed["answer"]

    assert route_after_graph({**state, "_graph_hit": True}) == "assemble"
    assert route_after_graph({
        "question": "莲子有什么营养",
        "_graph_hit": False,
        "_intents": ["nutrition"],
    }) == "vector_fallback"
    assert route_after_graph({**state, "question": "我怀孕了，猪脚莲子煲汤可以吗"}) == "vector_fallback"

    monkeypatch.setattr(config, "WEB_SEARCH_ENABLED", False)
    assert route_after_graph(state) == "vector_fallback"


def test_trim_keeps_a_short_remainder_and_ignores_superlative():
    kept, hits = trim_sentences("天麻可治疗头痛。天麻炖猪脚是家常做法。")
    assert kept == "天麻炖猪脚是家常做法。"
    assert len(kept) < config.WEB_ANSWER_MIN_CHARS
    assert any(hit.category == "medical_claim" for hit in hits)

    raw = "天麻可治疗头痛。天麻炖猪脚是家常做法。先焯水再下锅，汤色才会清，这是家常步骤，盐少放。"
    assert len(raw) >= config.WEB_ANSWER_MIN_CHARS
    rows, _, _ = _filter([_row(raw)])
    assert rows and "治疗" not in rows[0].content
    assert "家常做法" in rows[0].content

    short = _filter([_row("天麻炖猪脚是家常做法。")])
    assert short[0] == []

    kept_best, _ = trim_sentences("猪脚不宜和海鲜同煮。最好先焯水。")
    assert kept_best == "最好先焯水。"
    assert "不宜" not in kept_best


def test_prompt_caps_confidence_and_has_no_profile():
    import inspect

    assert "profile" not in inspect.signature(build_web_prompt).parameters
    prompt = build_web_prompt(SAMPLE, [{"title": "t", "content": COOKING, "site": "a"}], herbal=True)
    assert "0.25" in prompt
    assert "0.0-1.0" not in prompt
    assert "剂量" in prompt
    assert is_herbal(SAMPLE)
    assert _clamp(0.9) == 0.25
    assert _clamp(-1) == 0.0


def test_frontend_drops_the_false_promise():
    root = Path(__file__).resolve().parents[1]
    js = (root / "frontend/assets/app.js").read_text(encoding="utf-8")
    html = (root / "frontend/index.html").read_text(encoding="utf-8")
    assert "网络参考" in js
    assert "审核通过后就能查到" not in js
    assert "不会给出未核实的内容" not in js
    assert "20261030a" in html
    assert "noopener noreferrer" in js


def test_search_maps_title_link_and_host():
    class Hit:
        def __init__(self, **fields):
            self.__dict__.update(fields)

    class Resp:
        def __init__(self, rows):
            self.search_result = rows

    class Web:
        def __init__(self, rows):
            self.rows = rows

        def web_search(self, **kwargs):
            return Resp(self.rows)

    class Sdk:
        def __init__(self, rows):
            self.web_search = Web(rows)

    mapped = search(
        "天麻",
        client=Sdk([Hit(title="标题", content="正文", link="https://news.example.com/a", media="示例网")]),
        timeout_s=1,
    )
    assert mapped[0].title == "标题"
    assert mapped[0].url == "https://news.example.com/a"
    assert mapped[0].site == "news.example.com"

    no_link = search(
        "天麻",
        client=Sdk([Hit(title="标题", content="正文", media="示例网")]),
        timeout_s=1,
    )
    assert no_link[0].site == "示例网"


def test_search_timeout_does_not_wait_for_the_sdk():
    class Slow:
        def web_search(self, **kwargs):
            import time
            time.sleep(2)
            return None

    class Sdk:
        web_search = Slow()

    with pytest.raises(WebSearchError) as caught:
        search("天麻", client=Sdk(), timeout_s=0.2)
    assert str(caught.value) == "timeout"


# ---------------------------------------------------------------
# 要登记、要缓存，所以要测试库
# ---------------------------------------------------------------
@requires_pg
def test_sample_question_gets_a_temporary_web_answer(schema, calls):
    box, fake_search, fake_generate = calls
    user = "web-v4-sample"
    _purge(SAMPLE, user)
    try:
        out = answer(
            _state(SAMPLE, user=user),
            search_fn=fake_search,
            generate_fn=fake_generate,
        )
        assert out["_web_answer"]["used"] is True
        assert out["_graph_hit"] is False
        assert "未经审核" in out["answer"]
        assert out["_web_answer"]["sources"]
        assert [item["food_key"] for item in out["_food_requests"]] == ["猪脚", "天麻"]
        assert out["confidence"] <= 0.25
        assert "cook.example.com" in out["answer"]
    finally:
        _purge(SAMPLE, user)


@requires_pg
def test_political_snippet_drops_the_whole_row(schema, calls):
    _, _, fake_generate = calls
    user = "web-v4-pol-snip"
    question = "测甲乙丙怎么吃"
    _purge(question, user)

    def political_search(query):
        return [_row("这段讲的是台独。" + COOKING)]

    try:
        out = answer(
            _state(question, user=user),
            search_fn=political_search,
            generate_fn=fake_generate,
        )
        assert out["_web_answer"]["used"] is False
        assert out["_web_answer"]["blocked"] == "L2"
        assert "台独" not in out["answer"]
        assert "暂未收录" in out["answer"]
        import db

        with db.connect() as conn:
            row = conn.execute(
                "SELECT blocked FROM web_search_log WHERE query_hash = %s",
                (cache_key(question),),
            ).fetchone()
        assert row[0] == "L2"
    finally:
        _purge(question, user)


@requires_pg
def test_political_question_never_searches(schema, client, monkeypatch):
    import db
    from kg.sensitive import refresh

    refresh()
    question = "台独能当食材吗"
    user = "web-v4-guard"
    _purge(question, user)
    searched = []
    monkeypatch.setattr(
        "kg.webanswer._search",
        lambda query: searched.append(query) or [_row(COOKING)],
    )
    try:
        response = client.post("/chat", json={"question": question, "user_id": user})
        assert response.status_code == 200, response.text
        body = response.json()
        assert body["answer"] == POLITICAL_ANSWER
        assert "台独" not in body["answer"]
        assert searched == []
        assert _log_count(question) == 0
        assert _cache_count(question) == 0
        with db.connect() as conn:
            row = conn.execute(
                "SELECT count(*) FROM web_search_log WHERE user_id = %s",
                (user,),
            ).fetchone()
        assert int(row[0]) == 0
    finally:
        _purge(question, user)


@requires_pg
def test_failures_do_not_write_a_negative_cache(schema, calls, monkeypatch):
    box, _, fake_generate = calls
    question = "测甲乙丙怎么吃"
    user = "web-v4-fail"
    _purge(question, user)
    try:
        out = answer(
            _state(question, user=user),
            search_fn=lambda query: (_ for _ in ()).throw(WebSearchError("timeout")),
            generate_fn=fake_generate,
        )
        assert "暂未收录" in out["answer"]
        assert _cache_count(question) == 0

        _purge(question, user)
        out = answer(
            _state(question, user=user),
            search_fn=lambda query: (_ for _ in ()).throw(RuntimeError("sdk down")),
            generate_fn=fake_generate,
        )
        assert out["_web_answer"]["used"] is False
        assert _cache_count(question) == 0

        monkeypatch.setattr(config, "WEB_SEARCH_API_KEY", "")
        monkeypatch.setattr("kg.webanswer._search", lambda query: box["search"].append(query))
        _purge(question, user)
        out = answer(_state(question, user=user), generate_fn=fake_generate)
        assert box["search"] == []
        assert _cache_count(question) == 0
        import db

        with db.connect() as conn:
            row = conn.execute(
                "SELECT error FROM web_search_log WHERE query_hash = %s",
                (cache_key(question),),
            ).fetchone()
        assert row[0] == "no_key"

        monkeypatch.setattr(config, "WEB_ANSWER_DAILY_BUDGET", 0)
        _purge(question, user)
        out = answer(
            _state(question, user=user),
            search_fn=lambda query: box["search"].append(query) or [_row(COOKING)],
            generate_fn=fake_generate,
        )
        assert out["_web_answer"]["blocked"] == "budget"
        assert box["search"] == []
        assert _cache_count(question) == 0
    finally:
        _purge(question, user)


@requires_pg
def test_rate_limit_ignores_budget_rows_and_blocks_real_attempts(schema, calls, monkeypatch):
    box, fake_search, fake_generate = calls
    question = "测甲乙丙怎么吃"
    user = "web-v4-rate"
    _purge(question, user)
    import db

    try:
        with db.connect() as conn:
            for _ in range(5):
                conn.execute(
                    "INSERT INTO web_search_log (user_id, query_hash, ok, blocked) "
                    "VALUES (%s, %s, true, '')",
                    (user, "other"),
                )
            conn.execute(
                "INSERT INTO web_search_log (user_id, query_hash, ok, blocked) "
                "VALUES (%s, %s, false, 'budget')",
                (user, "budget-row"),
            )
        # 5 次真搜索 + 1 条预算日志。上限 6 时还得放行，说明预算日志不占次数。
        monkeypatch.setattr(config, "WEB_ANSWER_USER_HOURLY", 6)
        out = answer(
            _state(question, user=user),
            search_fn=fake_search,
            generate_fn=fake_generate,
        )
        assert len(box["search"]) == 1
        assert out["_web_answer"]["used"] is True
        _purge(question)

        blocked_user = "web-v4-rate-block"
        with db.connect() as conn:
            for _ in range(5):
                conn.execute(
                    "INSERT INTO web_search_log (user_id, query_hash, ok, blocked) "
                    "VALUES (%s, %s, true, '')",
                    (blocked_user, "other"),
                )
        monkeypatch.setattr(config, "WEB_ANSWER_USER_HOURLY", 5)
        blocked = answer(
            _state(question, user=blocked_user),
            search_fn=fake_search,
            generate_fn=fake_generate,
        )
        assert len(box["search"]) == 1
        assert blocked["_web_answer"]["blocked"] == "rate"
        assert _cache_count(question) == 0
    finally:
        _purge(question, user)
        _purge(question, "web-v4-rate-block")


@requires_pg
def test_generate_failure_pastes_trimmed_snippets(schema):
    question = "测甲乙丙怎么吃"
    user = "web-v4-paste"
    raw = "天麻可治疗头痛。孕妇不宜长期服用。" + COOKING
    _purge(question, user)
    try:
        out = answer(
            _state(question, user=user),
            search_fn=lambda query: [_row(raw)],
            generate_fn=lambda prompt: (_ for _ in ()).throw(RuntimeError("no model")),
        )
        assert out["_web_answer"]["used"] is True
        assert "家常做法" in out["answer"]
        assert "未经审核" in out["answer"]
        for banned in ("治疗", "不宜", "长期服用"):
            assert banned not in out["answer"]
        assert "cook.example.com" in out["answer"]
    finally:
        _purge(question, user)


@requires_pg
def test_same_question_searches_once_until_prompt_changes(schema, calls, monkeypatch):
    box, fake_search, fake_generate = calls
    question = "测甲乙丙怎么吃"
    user = "web-v4-cache"
    _purge(question, user)
    monkeypatch.setattr(config, "WEB_ANSWER_PROMPT_VERSION", "v-test")
    _purge(question, user)
    try:
        for _ in range(3):
            out = answer(
                _state(question, user=user),
                search_fn=fake_search,
                generate_fn=fake_generate,
            )
            assert out["_web_answer"]["used"] is True
        assert len(box["search"]) == 1
        import db

        with db.connect() as conn:
            row = conn.execute(
                "SELECT hit_count FROM web_answer_cache WHERE query_hash = %s",
                (cache_key(question),),
            ).fetchone()
        assert row[0] == 2

        monkeypatch.setattr(config, "WEB_ANSWER_PROMPT_VERSION", "v-test-2")
        answer(
            _state(question, user=user),
            search_fn=fake_search,
            generate_fn=fake_generate,
        )
        assert len(box["search"]) == 2
    finally:
        monkeypatch.setattr(config, "WEB_ANSWER_PROMPT_VERSION", "v-test")
        _purge(question, user)
        monkeypatch.setattr(config, "WEB_ANSWER_PROMPT_VERSION", "v-test-2")
        _purge(question, user)


@requires_pg
def test_one_request_registers_once_when_search_fails(schema):
    question = "测甲乙丙怎么吃"
    user = "web-v4-once"
    _purge(question, user)
    try:
        answer(
            _state(question, user=user),
            search_fn=lambda query: (_ for _ in ()).throw(WebSearchError("timeout")),
        )
        import db

        with db.connect() as conn:
            row = conn.execute(
                "SELECT hit_count FROM kg_ingredient_request WHERE food_key = %s AND requested_by = %s",
                ("测甲乙丙", user),
            ).fetchone()
        assert row[0] == 1
    finally:
        _purge(question, user)


@requires_pg
def test_third_food_is_not_marked_registered(schema, calls):
    _, fake_search, _ = calls
    question = "猪脚、天麻、当归煲汤合适吗"
    user = "web-v4-three"
    _purge(question, user)

    def generate(prompt):
        return {"answer": "当归也可以一起炖。", "cited": [], "confidence": 0.2}

    try:
        out = answer(
            _state(question, user=user),
            search_fn=fake_search,
            generate_fn=generate,
        )
        head = out["answer"].split("以下是网络检索", 1)[0]
        assert "当归" not in head
        assert "猪脚" in head and "天麻" in head
        assert "已登记" in head
        assert [item["food_key"] for item in out["_food_requests"]] == ["猪脚", "天麻"]
        assert "当归" in out["answer"]
    finally:
        _purge(question, user)


def _named_food(question, llm=None):
    """不把「测甲乙丙是不是食材」交给线上模型。模型回 [] 时这条链路根本进不了未收录。"""
    return ["测甲乙丙"] if "测甲乙丙" in (question or "") else []


@requires_pg
def test_switch_off_does_not_search(schema, client, monkeypatch):
    monkeypatch.setattr("kg.segment.extract_foods", _named_food)
    monkeypatch.setattr(config, "WEB_SEARCH_ENABLED", False)
    searched = []
    monkeypatch.setattr(
        "kg.webanswer._search",
        lambda query: searched.append(query) or [_row(COOKING)],
    )
    question = "测甲乙丙怎么吃"
    user = "web-v4-off"
    _purge(question, user)
    try:
        response = client.post("/chat", json={"question": question, "user_id": user})
        assert response.status_code == 200, response.text
        body = response.json()
        assert body["web_answer"]["used"] is False
        assert searched == []
        assert "网络参考" not in body["answer"]
        assert "暂未收录" in body["answer"]
        assert _cache_count(question) == 0
    finally:
        _purge(question, user)


@requires_pg
def test_generation_trim_pastes_snippets_instead_of_medical_fallback(schema):
    question = "测甲乙丙怎么吃"
    user = "web-v4-l3"
    _purge(question, user)
    try:
        emptied = answer(
            _state(question, user=user),
            search_fn=lambda query: [_row(COOKING)],
            generate_fn=lambda prompt: {"answer": "这个可以治疗头痛。", "confidence": 0.9},
        )
        assert emptied["_web_answer"]["used"] is True
        assert "家常做法" in emptied["answer"]
        assert "治疗" not in emptied["answer"]
        assert MEDICAL_FALLBACK not in emptied["answer"]
        assert emptied["confidence"] <= 0.25

        _purge(question, user)
        blocked = answer(
            _state(question, user=user),
            search_fn=lambda query: [_row(COOKING)],
            generate_fn=lambda prompt: {"answer": "据说台独也能入菜。", "confidence": 0.2},
        )
        assert blocked["_web_answer"]["used"] is False
        assert blocked["_web_answer"]["blocked"] == "L3"
        assert "台独" not in blocked["answer"]
        assert "家常做法" not in blocked["answer"]
        assert _cache_count(question) == 0
    finally:
        _purge(question, user)


@requires_pg
def test_absolute_words_stay_out_of_the_sensitive_table(schema):
    import db
    from kg.sensitive import refresh, rewrite_answer

    refresh()
    with db.connect() as conn:
        row = conn.execute(
            "SELECT count(*) FROM sensitive_word WHERE word IN ('不宜', '禁用')"
        ).fetchone()
    assert int(row[0]) == 0
    kept, _ = rewrite_answer("痛风患者不宜多吃动物内脏。")
    assert "不宜" in kept
    trimmed, _ = trim_sentences("最好先焯水。")
    assert trimmed == "最好先焯水。"


@requires_pg
def test_web_hits_are_logged_as_hashes(schema):
    import db

    question = "测甲乙丙怎么吃"
    user = "web-v4-audit"
    raw = "天麻可治疗头痛。" + COOKING
    _purge(question, user)
    try:
        answer(
            _state(question, user=user),
            search_fn=lambda query: [_row(raw)],
            generate_fn=lambda prompt: {"answer": "天麻炖猪脚是家常做法。", "confidence": 0.1},
        )
        digest = term_hash("治疗")
        with db.connect() as conn:
            row = conn.execute(
                "SELECT scene, category, term_hash, source FROM sensitive_hit_log "
                "WHERE scene = 'web' AND term_hash = %s ORDER BY id DESC LIMIT 1",
                (digest,),
            ).fetchone()
            assert row is not None
            assert row[0] == "web"
            assert "治疗" not in "".join(str(part) for part in row)
            assert question not in "".join(str(part) for part in row)
            logged = conn.execute(
                "SELECT user_id, query_hash, error FROM web_search_log WHERE query_hash = %s",
                (cache_key(question),),
            ).fetchone()
        assert question not in "".join(str(part) for part in logged)
    finally:
        _purge(question, user)


@requires_pg
def test_timeout_returns_http_200(schema, client, monkeypatch):
    monkeypatch.setattr("kg.segment.extract_foods", _named_food)
    monkeypatch.setattr(
        "kg.webanswer._search",
        lambda query: (_ for _ in ()).throw(WebSearchError("timeout")),
    )
    question = "测甲乙丙怎么吃"
    user = "web-v4-http"
    _purge(question, user)
    try:
        response = client.post("/chat", json={"question": question, "user_id": user})
        assert response.status_code == 200, response.text
        body = response.json()
        assert body["web_answer"]["used"] is False
        assert "暂未收录" in body["answer"]
        assert _cache_count(question) == 0
    finally:
        _purge(question, user)
