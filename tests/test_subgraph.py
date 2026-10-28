"""子图构建（行 → 点边）与表格行
—— 这一层不连库，纯翻译，所以能做成不依赖 PG / Neo4j 的单元测试。
"""
from __future__ import annotations

import pytest

import kg.subgraph as sg
from kg.subgraph import build_facts, build_subgraph, parse_amount

pytestmark = pytest.mark.no_db


def test_parse_amount_recovers_value_from_claim_text():
    """补录链路只把数值写进 claim_text，关系上没有 amount。
    回捞要抓「最后一个 数字+单位」，跳过「每 100g」这种口径数字。"""
    assert parse_amount("黑豆每 100g 含蛋白质 36.49 g") == (36.49, "g")
    assert parse_amount("黑豆每 100g 含能量 446.0 kcal") == (446.0, "kcal")
    assert parse_amount("黑豆每 100g 含钠 2.0 mg") == (2.0, "mg")
    assert parse_amount("黑豆富含植物蛋白") == (None, "")


def test_facts_fallback_to_claim_when_amount_missing():
    """关系上没存 amount 时，facts 也要能从 claim_text 回捞出数值。"""
    rows = build_facts([
        {
            "intent": "nutrition", "cid": "NUT_00002", "name": "蛋白质",
            "unit": "g", "claim": "黑豆每 100g 含蛋白质 36.49 g",
            "etype": "traditional", "elevel": "C", "provisional": True,
            "_src_cid": "ING_00007", "_src_label": "Ingredient", "_src_name": "黑豆",
        }
    ])
    assert rows[0]["amount"] == 36.49 and rows[0]["unit"] == "g"


def _nutrition_rows():
    return [
        {
            "intent": "nutrition", "cid": "NUT_00002", "name": "蛋白质",
            "unit": "g", "amount": 36.49, "claim": "黑豆富含植物蛋白",
            "etype": "traditional", "elevel": "C", "provisional": True,
            "_src_cid": "ING_00007", "_src_label": "Ingredient", "_src_name": "黑豆",
        },
        {
            "intent": "nutrition", "cid": "NUT_00005", "name": "膳食纤维",
            "unit": "g", "amount": 9.9, "claim": "", "etype": "traditional",
            "elevel": "C", "provisional": True,
            "_src_cid": "ING_00007", "_src_label": "Ingredient", "_src_name": "黑豆",
        },
    ]


def test_facts_are_structured_not_text():
    rows = build_facts(_nutrition_rows())
    assert len(rows) == 2
    assert rows[0]["name"] == "蛋白质"
    assert rows[0]["amount"] == 36.49 and rows[0]["unit"] == "g"
    assert rows[0]["evidence_level"] == "C" and rows[0]["provisional"] is True


def test_subgraph_has_anchor_and_edges():
    entities = [
        {"canonical_id": "ING_00007", "label": "Ingredient", "name": "黑豆",
         "matched_alias": "黑豆", "score": 1.0, "method": "substring"},
    ]
    g = build_subgraph(entities, _nutrition_rows())
    assert g is not None

    ids = {n["id"] for n in g["nodes"]}
    assert {"ING_00007", "NUT_00002", "NUT_00005"} == ids

    anchor = next(n for n in g["nodes"] if n["id"] == "ING_00007")
    assert anchor["anchor"] is True and anchor["name"] == "黑豆"

    assert len(g["edges"]) == 2
    e = g["edges"][0]
    assert e["source"] == "ING_00007" and e["rel"] == "CONTAINS_NUTRIENT"
    assert e["rel_label"] == "含有"
    assert e["amount"] == 36.49 and e["provisional"] is True
    assert g["meta"]["anchors"] == ["ING_00007"]


def test_two_ingredients_keep_their_own_edges():
    """「A 和 B 能一起吃吗」：两条边要分别挂在各自的锚点上，不能全挂在第一个。"""
    rows = [
        {"intent": "incompatible", "cid": "ING_00002", "name": "菠菜",
         "claim": "", "reason": "草酸影响钙吸收", "etype": "anecdotal", "elevel": "D",
         "_src_cid": "ING_00003", "_src_label": "Ingredient", "_src_name": "豆腐"},
        {"intent": "pair", "cid": "ING_00001", "name": "西红柿",
         "claim": "", "effect": "维C 促进铁吸收", "etype": "measured", "elevel": "B",
         "_src_cid": "ING_00002", "_src_label": "Ingredient", "_src_name": "菠菜"},
    ]
    entities = [
        {"canonical_id": "ING_00003", "label": "Ingredient", "name": "豆腐",
         "matched_alias": "豆腐", "score": 1.0, "method": "substring"},
        {"canonical_id": "ING_00002", "label": "Ingredient", "name": "菠菜",
         "matched_alias": "菠菜", "score": 1.0, "method": "substring"},
    ]
    g = build_subgraph(entities, rows)
    pairs = {(e["source"], e["rel"], e["target"]) for e in g["edges"]}
    assert ("ING_00003", "INCOMPATIBLE_WITH", "ING_00002") in pairs
    assert ("ING_00002", "RECOMMEND_PAIR", "ING_00001") in pairs
    assert len(g["nodes"]) == 3


def test_no_graph_no_subgraph():
    assert build_subgraph([], []) is None
    assert build_facts([]) == []


def test_symptom_remedy_draws_population_and_symptom():
    rows = [{
        "intent": "symptom_remedy", "cid": "ING_00009", "name": "海鲜",
        "population": "痛风患者", "symptom": "关节痛",
        "claim": "高嘌呤", "etype": "guideline", "elevel": "A",
        "_src_cid": "SYM_00001", "_src_label": "Symptom", "_src_name": "关节痛",
    }]
    g = build_subgraph([], rows)
    labels = sorted(n["label"] for n in g["nodes"])
    assert labels == ["Ingredient", "Population", "Symptom"]
    rels = {e["rel"] for e in g["edges"]}
    assert rels == {"CONTRA_POPULATION", "HAS_SYMPTOM"}


def test_compare_many_merges_and_dedupes(monkeypatch):
    """多食材对照：三对关系合并进一张图，共同营养素节点只留一个。"""
    def fake_fetch(cid):
        return {"label": "Ingredient", "cid": cid, "name": {"A": "黑豆", "B": "菠菜", "C": "豆腐"}[cid]}

    def fake_relation(a, b, **kw):
        # A、B 都含 N（共同营养素），C 与 A 直接搭配
        if a == "A" and b == "B":
            return {
                "nodes": [
                    {"id": "A", "label": "Ingredient", "name": "黑豆", "anchor": True, "hop": 0, "props": {}},
                    {"id": "B", "label": "Ingredient", "name": "菠菜", "anchor": True, "hop": 0, "props": {}},
                    {"id": "N", "label": "Nutrient", "name": "铁", "anchor": False, "hop": 1, "props": {}},
                ],
                "edges": [
                    {"source": "A", "target": "N", "rel": "CONTAINS_NUTRIENT", "rel_label": "含有", "hop": 1},
                    {"source": "B", "target": "N", "rel": "CONTAINS_NUTRIENT", "rel_label": "含有", "hop": 1},
                ],
                "meta": {},
            }
        if a == "A" and b == "C":
            return {
                "nodes": [
                    {"id": "A", "label": "Ingredient", "name": "黑豆", "anchor": True, "hop": 0, "props": {}},
                    {"id": "C", "label": "Ingredient", "name": "豆腐", "anchor": True, "hop": 0, "props": {}},
                ],
                "edges": [
                    {"source": "A", "target": "C", "rel": "RECOMMEND_PAIR", "rel_label": "推荐搭配", "hop": 1},
                ],
                "meta": {},
            }
        return {"nodes": [], "edges": [], "meta": {}}

    monkeypatch.setattr(sg, "fetch_node", fake_fetch)
    monkeypatch.setattr(sg, "relation_between", fake_relation)

    g = sg.compare_many(["A", "B", "C"])

    ids = {n["id"] for n in g["nodes"]}
    assert ids == {"A", "B", "C", "N"}  # N 只出现一次，被去重合并

    anchors = [n["id"] for n in g["nodes"] if n["anchor"]]
    assert set(anchors) == {"A", "B", "C"}

    pairs = {(e["source"], e["target"]) for e in g["edges"]}
    assert ("A", "N") in pairs and ("B", "N") in pairs and ("A", "C") in pairs
    assert g["meta"]["kind"] == "compare"
    assert g["meta"]["pair_count"] == 3
