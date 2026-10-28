"""
tests/test_import_fcd.py —— 中国食物成分表抓取包 → 审核队列的映射层

这层只做三件「确定」的事：分类查表、能量 kJ→kcal、别名去标签。
任何一项错了都会静默污染上千条数据，所以逐项钉住。
"""
from __future__ import annotations

from kg import importer
from scripts import import_fcd


def _item(**over):
    base = {
        "food_id": 259,
        "cat": "谷类及制品",
        "name": "小麦粉(标准粉)",
        "alias": None,
        "english_name": None,
        "fetched_at": "2026-10-28T11:56:55+08:00",
        "nutrients": {
            "energy": {"value": 1478.0, "unit": "kJ"},
            "protein": {"value": 11.2, "unit": "g"},
            "fat": {"value": 1.5, "unit": "g"},
            "carbohydrate": {"value": 73.6, "unit": "g"},
            "calcium": {"value": 31.0, "unit": "mg"},
        },
    }
    base.update(over)
    return base


# ============================================================
# 分类
# ============================================================
def test_every_mapped_category_is_a_graph_category():
    bad = {k: v for k, v in import_fcd.CATEGORY_MAP.items() if v not in importer.CATEGORIES}
    assert not bad, f"映射到了枚举外的分类：{bad}"


def test_unknown_category_falls_back_to_other_instead_of_guessing():
    # 抓取包将来加了新分类，宁可归「其他」也不要瞎猜
    vals = import_fcd.to_row(_item(cat="螺蛳粉类及制品"))
    assert vals["category"] == "其他"


def test_all_21_source_categories_are_covered():
    # 抓取包现在是 21 个分类；漏一个就会静默变成「其他」
    src = {
        "谷类及制品", "薯类、淀粉及制品", "干豆类及制品", "蔬菜类及制品", "菌藻类",
        "水果类及制品", "坚果、种子类", "畜肉类及制品", "禽肉类及制品", "蛋类及制品",
        "乳类及制品", "鱼虾蟹贝类", "油脂类", "调味品类", "速食食品", "小吃、甜饼",
        "饮料类", "含酒精饮料", "糖、果脯和蜜饯、蜂蜜", "婴幼儿食品", "其它",
    }
    assert src == set(import_fcd.CATEGORY_MAP)


# ============================================================
# 能量单位
# ============================================================
def test_energy_kj_is_converted_to_kcal():
    vals = import_fcd.to_row(_item())
    # 1478 kJ / 4.184 = 353.3 kcal
    assert abs(vals["energy_kcal"] - 353.3) < 0.05


def test_converted_energy_passes_the_kcal_upper_bound():
    # 纯油脂 3700kJ ≈ 884kcal，必须过 900 的上限（转换错成不除会直接爆界）
    vals = import_fcd.to_row(
        _item(nutrients={"energy": {"value": 3700.0, "unit": "kJ"},
                         "protein": {"value": 0.0, "unit": "g"},
                         "fat": {"value": 100.0, "unit": "g"},
                         "carbohydrate": {"value": 0.0, "unit": "g"}})
    )
    assert 880 < vals["energy_kcal"] < 900


# ============================================================
# 别名
# ============================================================
def test_alias_strips_shangpin_prefix_and_period():
    vals = import_fcd.to_row(_item(alias="商品名：无锡酱排骨。"))
    assert vals["aliases"] == "无锡酱排骨"


def test_missing_alias_stays_empty():
    assert import_fcd.to_row(_item(alias=None))["aliases"] == ""


# ============================================================
# 缺测营养素
# ============================================================
def test_absent_nutrient_is_omitted_not_zero():
    vals = import_fcd.to_row(_item())
    assert "iron_mg" not in vals, "原表没有的项不能补 0"


def test_alcohol_without_macros_is_accepted_when_relaxed():
    """含酒精饮料原表没有蛋白/脂肪/碳水 —— 那是未测定，不是漏填。"""
    item = _item(
        name="二锅头",
        cat="含酒精饮料",
        nutrients={
            "energy": {"value": 1464.0, "unit": "kJ"},
            "sodium": {"value": 1.0, "unit": "mg"},
            "potassium": {"value": 2.0, "unit": "mg"},
            "calcium": {"value": 1.0, "unit": "mg"},
        },
    )
    r = import_fcd.build_results([item])[0]
    assert r.verdict in ("ok", "warn"), [m.text for m in r.messages]
    assert r.importable


def test_same_row_is_rejected_without_relax_required():
    """同一行在严格模式下必须被拒 —— 证明放松开关只影响机器导入那条路径。"""
    item = _item(
        name="二锅头",
        cat="含酒精饮料",
        nutrients={
            "energy": {"value": 1464.0, "unit": "kJ"},
            "sodium": {"value": 1.0, "unit": "mg"},
            "potassium": {"value": 2.0, "unit": "mg"},
            "calcium": {"value": 1.0, "unit": "mg"},
        },
    )
    vals = import_fcd.to_row(item)
    row = [vals.get(c.key, "") for c in importer.COLUMNS]
    strict = importer.validate_row(2, row, list(importer.COLUMNS))
    assert strict.verdict == "error"


def test_row_with_too_few_nutrients_is_still_rejected():
    """放松必填不等于放松「至少 4 项」：空行不能借道进来。"""
    item = _item(
        name="某空行",
        nutrients={"energy": {"value": 100.0, "unit": "kJ"}},
    )
    r = import_fcd.build_results([item])[0]
    assert r.verdict == "error"


# ============================================================
# 溯源
# ============================================================
def test_source_carries_the_original_food_id():
    vals = import_fcd.to_row(_item())
    assert "id=259" in vals["source"]
    assert "chinanutri" in vals["source"]


# ============================================================
# 营养素映射的完整性
# ============================================================
# 抓取包里出现的全部 31 个 key（实测自 foods.json）。
# 这张表是「不静默丢数据」的守门员：原表加了新列不更新这里 → 用例红，
# 而不是等到半年后发现某个营养素从来没进过图谱。
SOURCE_NUTRIENT_KEYS = {
    "edible_ratio", "water", "energy", "protein", "fat", "cholesterol", "ash",
    "carbohydrate", "dietary_fiber", "carotene", "vitamin_a", "alpha_te",
    "thiamin", "riboflavin", "niacin", "vitamin_c", "calcium", "phosphorus",
    "potassium", "sodium", "magnesium", "iron", "zinc", "selenium", "copper",
    "manganese", "iodine", "sfa", "mufa", "pufa", "fatty_acid_total",
}

# 明确不导的（不是漏，是有理由不导）
IGNORED_SOURCE_KEYS = {
    "edible_ratio",       # 食部，不是营养素
    "water",              # 水分
    "ash",                # 灰分
    "fatty_acid_total",   # 就是 sfa+mufa+pufa 的和，导了等于同一件事说两遍
}


def test_no_source_nutrient_key_is_silently_dropped():
    mapped = set(import_fcd.NUTRIENT_MAP) | {"energy"}
    assert mapped | IGNORED_SOURCE_KEYS == SOURCE_NUTRIENT_KEYS
    assert not (mapped & IGNORED_SOURCE_KEYS)


def test_every_nutrient_map_value_is_a_real_template_column():
    keys = {c.key for c in importer.COLUMNS}
    bad = {k: v for k, v in import_fcd.NUTRIENT_MAP.items() if v not in keys}
    assert not bad, f"映射到了模板里不存在的列：{bad}"


def test_second_batch_nutrients_are_imported_not_dropped():
    """第二批这 13 项以前全被丢弃（图谱没有节点 → resolve 返回 None）。

    磷在抓取包里的覆盖率是 92.3%，丢了它等于「每 10 个食材少 1 个数」。
    """
    vals = import_fcd.to_row(
        _item(
            nutrients={
                "energy": {"value": 1478.0, "unit": "kJ"},
                "protein": {"value": 11.2, "unit": "g"},
                "fat": {"value": 1.5, "unit": "g"},
                "carbohydrate": {"value": 73.6, "unit": "g"},
                "phosphorus": {"value": 167.0, "unit": "mg"},
                "thiamin": {"value": 0.28, "unit": "mg"},
                "riboflavin": {"value": 0.08, "unit": "mg"},
                "niacin": {"value": 2.0, "unit": "mg"},
                "selenium": {"value": 5.4, "unit": "μg"},
                "copper": {"value": 0.42, "unit": "mg"},
                "manganese": {"value": 1.56, "unit": "mg"},
                "iodine": {"value": 2.7, "unit": "μg"},
                "cholesterol": {"value": 0.0, "unit": "mg"},
                "alpha_te": {"value": 0.26, "unit": "mg"},
                "sfa": {"value": 24.1, "unit": "%"},
                "mufa": {"value": 11.5, "unit": "%"},
                "pufa": {"value": 30.3, "unit": "%"},
            }
        )
    )
    for key, want in (
        ("phosphorus_mg", 167.0), ("vitamin_b1_mg", 0.28), ("vitamin_b2_mg", 0.08),
        ("niacin_mg", 2.0), ("selenium_ug", 5.4), ("copper_mg", 0.42),
        ("manganese_mg", 1.56), ("iodine_ug", 2.7), ("vitamin_e_mg", 0.26),
        ("sfa_pct", 24.1), ("mufa_pct", 11.5), ("pufa_pct", 30.3),
    ):
        assert vals.get(key) == want, f"{key} 应该是 {want}，实际 {vals.get(key)}"
    # 0 是「检出为零」，必须保留，不能当成空值丢掉
    assert vals.get("cholesterol_mg") == 0.0


def test_fatty_acid_values_keep_their_own_unit_scale():
    """%E 是「占总能量百分比」，不许换算成克。

    100.8%（海苔）这种值之所以能过校验，正是因为它按 %E 判上限；
    一旦误当成克、或按 4/9/4 换算，既错又会撞上「脂肪(g) ≤ 100」的闸门。
    """
    vals = import_fcd.to_row(
        _item(nutrients={
            "energy": {"value": 1478.0, "unit": "kJ"},
            "sfa": {"value": 100.8, "unit": "%"},
        })
    )
    assert vals["sfa_pct"] == 100.8
    assert "fat_g" not in vals
