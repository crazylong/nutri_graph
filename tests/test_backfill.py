"""
tests/test_backfill.py —— 补边脚本的「算差集」部分

补边脚本要写库，但最容易错的是**算哪几条边缺**：算错就会覆盖已审过的数值，
或者把 3000 多条边补成 6000 条。这一步是纯函数，单独钉住。

不连库：只验证 build_plan 的输入输出。
"""
from __future__ import annotations

import pytest

from kg import importer
from scripts import backfill_nutrients as bf

pytestmark = pytest.mark.no_db


def _bundle(**over):
    base = {
        "id": 1,
        "food_key": importer.name_key("小麦粉(标准粉)"),
        "canonical_name": "小麦粉(标准粉)",
        "canonical_id": "ING_01000",
        "source_count": 1,
        "score_total": 0.8,
        "sources": [{"type": "file", "title": "中国食物成分表"}],
        "evidence_type": "measured",
    }
    base.update(over)
    return base


def _item(names: dict[str, float]) -> dict:
    return {
        "name": "小麦粉(标准粉)",
        "cat": "谷类及制品",
        "nutrients": {k: {"value": v, "unit": "mg"} for k, v in names.items()},
    }


def test_plan_covers_newly_added_nutrients():
    items = {importer.name_key("小麦粉(标准粉)"): _item(
        {"protein": 11.2, "phosphorus": 167.0, "thiamin": 0.28, "sfa": 24.1}
    )}
    # sfa 的单位在原表里是 %，与 mg 不同；这里单独构造
    items[importer.name_key("小麦粉(标准粉)")]["nutrients"]["sfa"]["unit"] = "%"

    plan, stats = bf.build_plan([_bundle()], items)
    got = {p["col_key"]: p for p in plan}

    assert got["phosphorus_mg"]["ref"].canonical_id == "NUT_00023"
    assert got["phosphorus_mg"]["value"] == 167.0
    assert got["vitamin_b1_mg"]["ref"].canonical_id == "NUT_00003"
    assert got["sfa_pct"]["ref"].canonical_id == "NUT_00032"
    assert got["sfa_pct"]["ref"].unit == "%E"
    assert stats["bundle_matched"] == 1


def test_plan_skips_bundles_without_source_record():
    plan, stats = bf.build_plan([_bundle(food_key="抓取包里没有的东西")], {})
    assert plan == []
    assert stats["bundle_without_source"] == 1


def test_plan_respects_key_filter():
    items = {importer.name_key("小麦粉(标准粉)"): _item(
        {"protein": 11.2, "phosphorus": 167.0}
    )}
    plan, _ = bf.build_plan([_bundle()], items, only_keys={"phosphorus_mg"})
    assert [p["col_key"] for p in plan] == ["phosphorus_mg"]


def test_plan_keeps_zero_values():
    """0 是「检出为零」（比如植物性食物的胆固醇），不是「没有数据」。"""
    items = {importer.name_key("小麦粉(标准粉)"): _item({"cholesterol": 0.0, "protein": 11.2})}
    plan, _ = bf.build_plan([_bundle()], items)
    chol = [p for p in plan if p["col_key"] == "cholesterol_mg"]
    assert len(chol) == 1 and chol[0]["value"] == 0.0


def test_plan_passes_provenance_through():
    items = {importer.name_key("小麦粉(标准粉)"): _item({"phosphorus": 167.0})}
    plan, _ = bf.build_plan([_bundle(id=42, canonical_id="ING_01000")], items)
    p = plan[0]
    assert p["src_id"] == "ING_01000"
    assert p["bundle_id"] == 42
    assert p["sources"][0]["title"] == "中国食物成分表"
