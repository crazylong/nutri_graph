"""未收录食材的登记链路（Phase 9.1）"""
from __future__ import annotations

import psycopg
import pytest

import config
from kg.request import (
    extract_candidates,
    is_plausible_food,
    normalize_food_term,
    register,
)
from tests.helpers import hide_aliases, bearer, login, rand_phone, requires_pg

pytestmark = requires_pg


def _alias(entity_type: str, canonical_id: str, alias: str) -> None:
    with psycopg.connect(config.PG_DSN) as conn:
        conn.execute(
            "INSERT INTO dict_alias (entity_type, canonical_id, alias, alias_norm) "
            "VALUES (%s, %s, %s, %s) ON CONFLICT DO NOTHING",
            (entity_type, canonical_id, alias, alias),
        )


def _reset_request_table() -> None:
    with psycopg.connect(config.PG_DSN) as conn:
        # CASCADE：kg_fetch_task / kg_food_bundle 都引用这张表
        conn.execute("TRUNCATE kg_ingredient_request CASCADE")


@pytest.fixture(autouse=True)
def _isolate():
    """每个用例开始前清掉本文件可能残留的测试数据。

    没有这一步，用例会互相污染：某个用例往 dict_alias 插了「黑豆」，
    后面的「黑豆」用例就直接变成 catalogued，第二次跑整组全红。
    只删带 TEST_ 前缀的 canonical_id，不动真实数据。
    """
    with psycopg.connect(config.PG_DSN) as conn:
        conn.execute(
            "DELETE FROM dict_alias WHERE canonical_id LIKE 'ING_TEST_%' "
            "OR canonical_id LIKE 'POP_TEST_%'"
        )
        conn.execute(
            "DELETE FROM dict_alias_candidate WHERE suggested_canonical_id LIKE 'ING_TEST_%'"
        )
        # 「黑大豆」是本文件的核心用例词。候选行可能被别的测试（test_promote 审核
        # 一个叫「黑大豆」的 bundle）写成 approved，而 approved 的候选按设计不会被
        # 新的启发式覆盖 —— 不清掉，同名断言就会拿到别人的建议。
        conn.execute(
            "DELETE FROM dict_alias_candidate WHERE alias_norm = %s", ("黑大豆",)
        )
        conn.execute("TRUNCATE kg_ingredient_request CASCADE")
    yield


# ============================================================
# 纯函数
# ============================================================
@pytest.mark.parametrize(
    "raw,center",
    [
        ("黑豆粥", "黑豆"),
        ("炒黑豆", "黑豆"),
        ("凉拌黑木耳", "黑木耳"),
        ("黑芝麻糊", "黑芝麻"),
        ("豆干", "豆干"),      # 剥掉「干」只剩一个字，不动
        ("黑豆", "黑豆"),
    ],
)
def test_strip_cooking_affixes(raw, center):
    got, _method = normalize_food_term(raw)
    assert got == center


@pytest.mark.parametrize("term,ok", [
    ("黑豆", True),
    ("鹰嘴豆", True),
    ("嘌呤", False),        # 营养素，不是食材
    ("热量", False),
    ("黑", False),          # 太短
    ("123", False),
    ("黑豆！", False),
])
def test_plausible_food(term, ok):
    assert is_plausible_food(term) is ok


def test_extract_candidates_from_residual():
    # 没有被任何别名占用时，residual 就是整句
    assert extract_candidates("黑豆和什么一起吃好") == ["黑豆"]
    # 被别名占掉的位置是 \x00，不能当候选
    assert extract_candidates("黑豆\x00\x00\x00和什么一起吃好") == ["黑豆"]
    # 营养词会被过滤掉，只留食材
    assert extract_candidates("芦笋含嘌呤高吗") == ["芦笋"]


@pytest.mark.parametrize("question,expect", [
    ("红烧肉配什么好", ["红烧肉"]),
    ("鹰嘴豆怎么做", ["鹰嘴豆"]),
    ("黑豆粥怎么煮", ["黑豆粥"]),        # 这一层不管归一，只负责切干净
    ("藜麦合适吗", ["藜麦"]),
])
def test_extract_candidates_does_not_leave_pseudo_food(question, expect):
    """回归：曾经把「红烧肉配什么好」切成「红烧肉配」，再剥成「肉配」登记进去。"""
    assert extract_candidates(question) == expect


def test_normalize_refuses_untrustworthy_strip():
    """剥离后若残留强停顿词，说明分段没切干净，宁可放弃剥离。"""
    assert normalize_food_term("红烧肉配") == ("红烧肉配", "asis")
    assert normalize_food_term("黑豆合适") == ("黑豆合适", "asis")
    # 正常词不受影响
    assert normalize_food_term("黑豆粥") == ("黑豆", "strip_suffix:粥")


# 登记类的用例必须用**图谱里不可能存在**的词。
# 用「黑豆」的老写法在生产库上会直接失效：它已经真的被补录进主图了，
# register 第一步就返回 catalogued —— 测试套件不该依赖「库是空的」。
# 纯函数用例（normalize_food_term / extract_candidates）继续用中文真词，那里不查库。
UNSEEN = "ZTEST谷"          # 6 字：2~8 的合法区间
UNSEEN_COMPOSITE = "ZTEST谷粥"   # 归一到 UNSEEN
UNSEEN_PREFIXED = "炒ZTEST谷"

# ============================================================
# 登记：三条出口
# ============================================================
def test_new_request_is_idempotent(client):
    _reset_request_table()
    first = register(UNSEEN, who="u-test")
    assert first.action == "new_request"
    assert first.request_id is not None
    assert first.hit_count == 1
    assert "暂未收录" in first.message and "已登记" in first.message

    again = register(UNSEEN, who="u-test")
    assert again.action == "duplicated"
    assert again.request_id == first.request_id
    assert again.hit_count == 2, "重复提交应该只累加 hit_count，不能产生新记录"

    with psycopg.connect(config.PG_DSN) as conn:
        n = conn.execute("SELECT count(*) FROM kg_ingredient_request").fetchone()[0]
    assert n == 1


def test_composite_term_collapses_to_one_request(client):
    """中心词 / 带后缀 / 带前缀 三种说法归到同一个 food_key。"""
    _reset_request_table()
    register(UNSEEN, who="u-test")
    register(UNSEEN_COMPOSITE, who="u-test")
    register(UNSEEN_PREFIXED, who="u-test")
    with psycopg.connect(config.PG_DSN) as conn:
        rows = conn.execute("SELECT food_key, hit_count FROM kg_ingredient_request").fetchall()
    assert len(rows) == 1 and rows[0][0] == UNSEEN and rows[0][1] == 3


def test_alias_candidate_instead_of_new_request(client):
    """「黑大豆」很像已有的「黑豆」→ 反问 + 记别名候选，不建新条目。"""
    _reset_request_table()
    with hide_aliases("黑豆"):
        _alias("Ingredient", "ING_TEST_BLACKBEAN", "黑豆")
        result = register("黑大豆", who="u-test")
        assert result.action == "alias_candidate"
        assert result.suggestion["canonical_id"] == "ING_TEST_BLACKBEAN"
        assert "黑豆" in result.message

        with psycopg.connect(config.PG_DSN) as conn:
            assert conn.execute("SELECT count(*) FROM kg_ingredient_request").fetchone()[0] == 0
            cand = conn.execute(
                "SELECT suggested_canonical_id FROM dict_alias_candidate WHERE alias = %s",
                ("黑大豆",),
            ).fetchone()
    assert cand and cand[0] == "ING_TEST_BLACKBEAN"


def test_already_catalogued(client):
    _reset_request_table()
    _alias("Ingredient", "ING_TEST_CAT", "荞麦")
    result = register("荞麦", who="u-test")
    assert result.action == "catalogued"


def test_non_ingredient_entity_rejected(client):
    """「痛风」是人群，不是食材 —— 直接挡掉并提示换个问法。"""
    _reset_request_table()
    _alias("Population", "POP_TEST_GICHT", "痛风")
    result = register("痛风", who="u-test")
    assert result.action == "rejected_not_ingredient"
    assert result.extra["entity_type"] == "Population"
    assert "不是食材" in result.message


def test_gibberish_rejected(client):
    _reset_request_table()
    assert register("？？？", who="u-test").action == "rejected_not_food"
    assert register("1234", who="u-test").action == "rejected_not_food"


def test_user_daily_limit(client, monkeypatch):
    _reset_request_table()
    monkeypatch.setattr(config, "FOOD_REQUEST_DAILY_LIMIT_PER_USER", 1)
    assert register("藜麦", who="u-limit").action == "new_request"
    over = register("秋葵", who="u-limit")
    assert over.action == "rate_limited"
    assert over.code == "user_daily_limit"


def test_rate_limit_does_not_block_repeat(client, monkeypatch):
    """限流只挡新条目；重复问同一个词仍然累加热度。"""
    _reset_request_table()
    monkeypatch.setattr(config, "FOOD_REQUEST_DAILY_LIMIT_PER_USER", 1)
    register("藜麦", who="u-limit2")
    again = register("藜麦", who="u-limit2")
    assert again.action == "duplicated" and again.hit_count == 2


def test_register_never_raises(client, monkeypatch):
    """登记失败不能把 /chat 带崩。"""
    monkeypatch.setattr(config, "PG_DSN", "postgresql://nobody:bad@127.0.0.1:1/none")
    result = register("黑豆", who="u-test")
    assert result.action == "not_ready"
    assert result.message  # 仍然给用户一句能看懂的话


# ============================================================
# API
# ============================================================
def test_api_submit_and_query(client):
    _reset_request_table()
    tokens = login(client)
    h = bearer(tokens["access_token"])

    r = client.post("/kg/food/request", json={"food": "鹰嘴豆"}, headers=h)
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["action"] == "new_request"
    rid = body["request_id"]

    r = client.get(f"/kg/food/request/{rid}", headers=h)
    assert r.status_code == 200
    assert r.json()["food_key"] == "鹰嘴豆"
    assert r.json()["status"] == "queued"
    assert "已登记" in r.json()["status_text"]

    # 别人查不到（不暴露存在性）
    other = login(client)
    r = client.get(f"/kg/food/request/{rid}", headers=bearer(other["access_token"]))
    assert r.status_code == 404


def test_chat_routes_unknown_food_to_uncatalogued(client):
    """整条链路：/chat 问一个不在图谱里的食材 → 走 uncatalogued 分支并登记。

    用「火龙果」而不是「黑豆」：前面的用例往 dict_alias 里插了「黑豆」别名，
    如果这里再用黑豆就会命中 Ingredient 分支、去连 Neo4j。
    """
    _reset_request_table()
    r = client.post("/chat", json={"question": "火龙果和什么一起吃好？"})
    assert r.status_code == 200, r.text
    body = r.json()
    assert "暂未收录" in body["answer"], body["answer"]
    assert "火龙果" in body["answer"]
    assert body["evidence_counts"] == {}
    # 结构化返回，前端不用从话术里抠编号
    assert body["food_requests"][0]["food_key"] == "火龙果"
    assert body["food_requests"][0]["action"] == "new_request"

    with psycopg.connect(config.PG_DSN) as conn:
        row = conn.execute(
            "SELECT food_key, status FROM kg_ingredient_request WHERE food_key = %s",
            ("火龙果",),
        ).fetchone()
    assert row == ("火龙果", "queued")


def test_candidate_suggestion_follows_latest_evidence(client):
    """待确认的别名建议要跟着图谱变，不能被第一条永久占位。

    DO NOTHING 的老行为：今天「黑大豆」最像「黑豆」，明天真·黑大豆补录进来了，
    再问一次仍然指向旧节点 —— 而审核的人看到的是过期建议。
    """
    _reset_request_table()
    with hide_aliases("黑豆"):
        _alias("Ingredient", "ING_TEST_OLD", "黑豆")
        register("黑大豆", who="u-test")

        with psycopg.connect(config.PG_DSN) as conn:
            first = conn.execute(
                "SELECT suggested_canonical_id, status FROM dict_alias_candidate "
                "WHERE alias = %s",
                ("黑大豆",),
            ).fetchone()
            # 图谱变了：这个别名换挂到另一个节点
            conn.execute(
                "UPDATE dict_alias SET canonical_id = %s WHERE canonical_id = %s",
                ("ING_TEST_NEW", "ING_TEST_OLD"),
            )
        assert first[0] == "ING_TEST_OLD" and first[1] == "pending"

        register("黑大豆", who="u-test")
        with psycopg.connect(config.PG_DSN) as conn:
            second = conn.execute(
                "SELECT suggested_canonical_id FROM dict_alias_candidate WHERE alias = %s",
                ("黑大豆",),
            ).fetchone()
    assert second[0] == "ING_TEST_NEW"


def test_reviewed_candidate_is_frozen(client):
    """已审核的结论不能被后来的启发式覆盖 —— 那是意见，不是缓存。"""
    _reset_request_table()
    with hide_aliases("黑豆"):
        _alias("Ingredient", "ING_TEST_OK", "黑豆")
        register("黑大豆", who="u-test")

        with psycopg.connect(config.PG_DSN) as conn:
            conn.execute(
                "UPDATE dict_alias_candidate SET status = 'approved' WHERE alias = %s",
                ("黑大豆",),
            )
            conn.execute(
                "UPDATE dict_alias SET canonical_id = %s WHERE canonical_id = %s",
                ("ING_TEST_OTHER", "ING_TEST_OK"),
            )

        register("黑大豆", who="u-test")
        with psycopg.connect(config.PG_DSN) as conn:
            row = conn.execute(
                "SELECT suggested_canonical_id FROM dict_alias_candidate WHERE alias = %s",
                ("黑大豆",),
            ).fetchone()
    assert row[0] == "ING_TEST_OK"
