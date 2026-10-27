"""抓取与校验（Phase 9.2）：同名异物、评分、流水线"""
from __future__ import annotations

import psycopg
import pytest

import config
from kg import fetch, usda
from kg.draft import FoodDraft
from kg.verify import verify
from tests.helpers import hide_aliases, requires_pg

pytestmark = requires_pg

SOYBEAN = {
    "fdcId": 174270,
    "description": "Soybeans, mature seeds, raw",
    "dataType": "SR Legacy",
    "foodCategory": "Legumes and Legume Products",
}
BLACK_BEAN = {
    "fdcId": 173734,
    "description": "Beans, black, mature seeds, raw",
    "dataType": "SR Legacy",
    "foodCategory": "Legumes and Legume Products",
}


def _detail(fdc_id=174270, desc="Soybeans, mature seeds, raw") -> dict:
    """真实的详情端点结构：nutrient 是嵌套对象，foodCategory 是对象。"""
    return {
        "fdcId": fdc_id,
        "description": desc,
        "foodCategory": {"id": 16, "code": "1600", "description": "Legumes and Legume Products"},
        "foodNutrients": [
            {"nutrient": {"id": 1008, "number": "208", "unitName": "kcal"}, "amount": 446.0},
            {"nutrient": {"id": 1003, "number": "203", "unitName": "g"}, "amount": 36.49},
            {"nutrient": {"id": 1004, "number": "204", "unitName": "g"}, "amount": 19.94},
            {"nutrient": {"id": 1005, "number": "205", "unitName": "g"}, "amount": 30.16},
            {"nutrient": {"id": 1079, "number": "291", "unitName": "g"}, "amount": 9.3},
            {"nutrient": {"id": 1093, "number": "307", "unitName": "mg"}, "amount": 2.0},
            {"nutrient": {"id": 9999, "number": "000", "unitName": "g"}, "amount": 1.0},  # 不关心
        ],
    }


class FakeUsda:
    """替身客户端：形状完全按实测的真实响应来构造。"""

    def __init__(self, foods, detail=None):
        self.foods = foods
        self.detail = detail or _detail()
        self.searched = []

    def search(self, query, page_size=8):
        self.searched.append(query)
        return self.foods

    def get_food(self, fdc_id):
        return self.detail


@pytest.fixture(autouse=True)
def _iso():
    """每例清场，并在用例期间摘掉「黑豆」的既有别名。

    第二件事很关键：流水线用例要的是「一个还没收录的食材」这个前提。
    部署环境的库里「黑豆」可能已经补录进主图了，那时 _alias_conflict 会给
    分数扣掉 0.2 并加一条硬否决（0.88 -> 0.68），断言 0.88 就会挂。
    摘掉再放回，前提就稳定了（见 tests/helpers.hide_aliases）。
    """
    with psycopg.connect(config.PG_DSN) as conn:
        # 一条语句搞定：TRUNCATE 检查的是外键约束（不是行数），
        # 被引用的表必须一起截断，否则报 FeatureNotSupported。
        # kg_node_candidate 是 9.3 加的，它引用 kg_food_bundle，漏了它整组报错。
        conn.execute(
            "TRUNCATE kg_node_candidate, kg_food_bundle_item, kg_food_bundle, "
            "kg_fetch_task, kg_ingredient_request"
        )
        conn.execute("DELETE FROM dict_alias WHERE canonical_id LIKE 'ING_TEST_%'")
    with hide_aliases("黑豆"):
        yield


# ============================================================
# 同名异物：这一步是整个数据正确性的关键
# ============================================================
def test_choose_picks_soybean_not_black_bean():
    d = FoodDraft(
        canonical_name="黑豆", english_name="soybeans",
        fdc_query="soybeans mature seeds raw",
        identity_keywords=["soybean"],
        exclude_keywords=["black", "turtle"],
        scientific_name="Glycine max", category="豆类",
    )
    pick, veto = fetch.choose(d, [BLACK_BEAN, SOYBEAN])
    assert pick is not None and pick["fdcId"] == 174270, "应该选中大豆，而不是黑菜豆"
    assert veto == ""


def test_choose_vetoes_when_only_wrong_species():
    """只有黑菜豆时不能"凑合选一个"，必须否决。"""
    d = FoodDraft(
        canonical_name="黑豆", english_name="soybeans",
        fdc_query="soybeans mature seeds raw",
        identity_keywords=["soybean"], exclude_keywords=["black"],
        scientific_name="Glycine max", category="豆类",
    )
    pick, veto = fetch.choose(d, [BLACK_BEAN])
    assert pick is None
    assert "不含身份关键词" in veto


def test_choose_excludes_even_if_identity_matches():
    """身份词命中但含排除词，也要否决（模型的英文名可能翻错）。"""
    d = FoodDraft(
        canonical_name="黑豆", english_name="black beans",
        fdc_query="black beans",
        identity_keywords=["bean"],            # 故意用宽泛词
        exclude_keywords=["black"],
        scientific_name="Glycine max", category="豆类",
    )
    pick, veto = fetch.choose(d, [BLACK_BEAN])
    assert pick is None
    assert "同名异物" in veto


def test_category_cannot_distinguish_them():
    """留个证据：两者 foodCategory 完全相同，所以分类校验帮不上忙。"""
    assert BLACK_BEAN["foodCategory"] == SOYBEAN["foodCategory"]


# ============================================================
# 解析与评分
# ============================================================
def test_parse_nutrients_uses_nutrient_id_not_number():
    nutrients = usda.parse_nutrients(_detail())
    keys = [n["key"] for n in nutrients]
    assert "energy_kcal" in keys, "必须按 nutrient.id=1008 匹配，不是老的 208"
    assert len(nutrients) == 6, "不关心的营养素要忽略"
    assert nutrients[0]["key"] == "energy_kcal"
    assert nutrients[0]["amount"] == 446.0


def test_category_description_handles_both_shapes():
    assert usda.category_description(_detail()) == "Legumes and Legume Products"
    assert usda.category_description(SOYBEAN) == "Legumes and Legume Products"


def test_verify_single_source_capped_below_threshold():
    """单源一致性 0.6 → 0.88 < 0.9，必然进人工。"""
    d = FoodDraft("黑豆", "soybeans", "q", ["soybean"], [], "Glycine max", "豆类").as_dict()
    vr = verify(d, usda.parse_nutrients(_detail()), source_key="usda_fdc", source_count=1)
    assert vr.score_total == 0.88
    assert vr.status == "pending_review"
    assert not vr.hard_veto


def test_verify_two_sources_can_auto_approve():
    d = FoodDraft("黑豆", "soybeans", "q", ["soybean"], [], "Glycine max", "豆类").as_dict()
    vr = verify(d, usda.parse_nutrients(_detail()), source_key="usda_fdc", source_count=2)
    assert vr.score_total == 1.0
    assert vr.status == "approved"


def test_verify_vetoes_on_missing_fields_and_unknown_source():
    d = {"canonical_name": "黑豆", "english_name": "", "category": ""}
    vr = verify(d, [], source_key="random_blog", source_count=1)
    assert "必需字段缺失" in vr.hard_veto
    assert "营养素不足" in vr.hard_veto
    assert "白名单" in vr.hard_veto
    assert vr.status == "pending_review"


# ============================================================
# 流水线
# ============================================================
def _seed(food: str = "黑豆") -> int:
    """直接插 request + task，**不走 register**。

    为什么不走：register 会先查图谱，而「黑豆」在部署环境的库里可能真的已经补录进去了
    —— 那一步会返回 catalogued，流水线测试连任务都建不出来。
    这一组要验的是 fetch 本身（物种核对 / 打分 / 落 bundle），
    不该被「图谱里有什么」左右。
    """
    with psycopg.connect(config.PG_DSN) as conn:
        rid = conn.execute(
            "INSERT INTO kg_ingredient_request "
            "(food_key, raw_mention, normalized, requested_by, source, status) "
            "VALUES (%s, %s, %s, 'u-fetch', 'test', 'queued') RETURNING id",
            (food, food, food),
        ).fetchone()[0]
        tid = conn.execute(
            "INSERT INTO kg_fetch_task (request_id, food_key) VALUES (%s, %s) "
            "RETURNING id",
            (rid, food),
        ).fetchone()[0]
    return tid


def test_register_creates_one_task_and_stays_idempotent():
    from kg.request import register

    register("ZTEST谷", who="u-fetch")
    register("ZTEST谷粥", who="u-fetch")   # 归一后仍是「ZTEST谷」
    with psycopg.connect(config.PG_DSN) as conn:
        n_task = conn.execute("SELECT count(*) FROM kg_fetch_task").fetchone()[0]
        n_req = conn.execute("SELECT count(*) FROM kg_ingredient_request").fetchone()[0]
    assert (n_req, n_task) == (1, 1)
    with psycopg.connect(config.PG_DSN) as conn:
        key = conn.execute("SELECT food_key FROM kg_ingredient_request").fetchone()[0]
    assert key == "ZTEST谷"


def test_pipeline_end_to_end():
    task_id = _seed("黑豆")
    fake = FakeUsda([BLACK_BEAN, SOYBEAN])

    results = fetch.run(limit=1, task_id=task_id, client=fake)
    assert len(results) == 1
    r = results[0]
    assert r["status"] == "done", r
    assert r["matched"] == "Soybeans, mature seeds, raw"
    assert r["nutrients"] == 6
    assert r["score"] == 0.88
    assert r["bundle_status"] == "pending_review"

    with psycopg.connect(config.PG_DSN) as conn:
        b = conn.execute(
            "SELECT food_key, canonical_name, scientific_name, external_id, status, source_count, "
            "nature FROM kg_food_bundle"
        ).fetchone()
        items = conn.execute(
            "SELECT count(*) FROM kg_food_bundle_item WHERE item_type = 'nutrient'"
        ).fetchone()[0]
        req_status = conn.execute("SELECT status FROM kg_ingredient_request").fetchone()[0]
        task_status = conn.execute("SELECT status FROM kg_fetch_task").fetchone()[0]

    assert b[0] == "黑豆" and b[2] == "Glycine max"
    assert b[3] == "174270" and b[4] == "pending_review" and b[5] == 1
    assert b[6] == "", "性味不该由模型编，必须是空等离线导入"
    assert items == 6
    assert req_status == "pending_review"
    assert task_status == "done"


def test_pipeline_rerun_overwrites_bundle():
    task_id = _seed("黑豆")
    fetch.run(limit=1, task_id=task_id, client=FakeUsda([SOYBEAN]))

    # 重新排队再跑一次
    with psycopg.connect(config.PG_DSN) as conn:
        conn.execute("UPDATE kg_fetch_task SET status = 'queued', attempts = 0")
    fetch.run(limit=1, task_id=task_id, client=FakeUsda([SOYBEAN]))

    with psycopg.connect(config.PG_DSN) as conn:
        n_bundle = conn.execute("SELECT count(*) FROM kg_food_bundle").fetchone()[0]
        n_item = conn.execute("SELECT count(*) FROM kg_food_bundle_item").fetchone()[0]
    assert n_bundle == 1, "同一请求只能有一份产物"
    assert n_item == 11, "重跑不能把 items 堆起来（5 字段 + 6 营养素）"


def test_pipeline_no_match_requeues_then_fails():
    task_id = _seed("黑豆")
    fake = FakeUsda([BLACK_BEAN])          # 只有错误的物种

    r = fetch.run(limit=1, task_id=task_id, client=fake)[0]
    assert r["status"] == "no_match"
    with psycopg.connect(config.PG_DSN) as conn:
        row = conn.execute("SELECT status, last_error FROM kg_fetch_task").fetchone()
    assert row[0] == "queued", "第 1 次失败应该重试而不是直接死掉"
    assert "不含身份关键词" in row[1]

    # 耗尽重试次数后置为 failed
    with psycopg.connect(config.PG_DSN) as conn:
        conn.execute("UPDATE kg_fetch_task SET attempts = 3, next_retry_at = NULL")
    r = fetch.run(limit=1, task_id=task_id, client=fake)[0]
    assert r["status"] == "no_match"
    with psycopg.connect(config.PG_DSN) as conn:
        assert conn.execute("SELECT status FROM kg_fetch_task").fetchone()[0] == "failed"


def test_recover_stale_tasks():
    task_id = _seed("黑豆")
    with psycopg.connect(config.PG_DSN) as conn:
        conn.execute(
            "UPDATE kg_fetch_task SET status = 'running', started_at = now() - interval '2 hours' "
            "WHERE id = %s",
            (task_id,),
        )
    assert fetch.recover_stale(15) == 1
    with psycopg.connect(config.PG_DSN) as conn:
        assert conn.execute("SELECT status FROM kg_fetch_task").fetchone()[0] == "queued"


def test_budget_blocks_claim(monkeypatch):
    _seed("黑豆")
    monkeypatch.setattr(config, "FOOD_FETCH_DAILY_BUDGET", 0)
    results = fetch.run(limit=1)
    assert results and results[0]["status"] == "budget_exhausted"


def test_usda_error_is_retryable():
    exc = usda.UsdaError("触发限流", retryable=True)
    assert exc.retryable is True
    assert usda.UsdaError("404", retryable=False).retryable is False
