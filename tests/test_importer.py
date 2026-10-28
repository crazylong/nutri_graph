"""
tests/test_importer.py —— 表格导入的解析与校验

这几条用例覆盖的都是「错得很贵」的地方：
  * 单位不符被静默换算 → 1000 倍误差
  * kJ 当 kcal → 4 倍误差
  * 分类自由发挥 → 导入后无法筛选
  * 可选营养素留空被判错 → 用户被迫把「没有」写成 0
  * 模板自己生成的表导不进自己 → 最丢人的一类 bug

全部是纯函数用例，不连库，任何环境都能跑。
"""
from __future__ import annotations

import csv
import io

import pytest

from kg import importer


def _mapped(headers: list[str]):
    return [importer.resolve_header(h) for h in headers]


def _row(**kw):
    """按 kw 拼一行，列顺序跟 COLUMNS 一致。

    没给的列一律算「留空」，而不是 KeyError：词表扩容时新增一列，
    不该让几十条与它无关的用例一起变红（那是用例的结构问题，不是数据问题）。
    """
    base = {
        "name": "藜麦",
        "aliases": "",
        "category": "谷物",
        "nature": "平",
        "english_name": "",
        "scientific_name": "",
        "energy_kcal": 368,
        "protein_g": 14.1,
        "fat_g": 6.1,
        "carb_g": 64.2,
        "fiber_g": 7.0,
        "sodium_mg": 5,
        "calcium_mg": 47,
        "iron_mg": 4.6,
        "zinc_mg": 3.1,
        "potassium_mg": 563,
        "magnesium_mg": 197,
        "vitamin_c_mg": 0,
        "vitamin_a_ug": 0,
        "carotene_ug": 0,
        "purine_mg": 0,
        "source": "中国食物成分表 第6版",
        "note": "",
    }
    base.update(kw)
    return [base.get(c.key, "") for c in importer.COLUMNS]


HEADERS = [c.header for c in importer.COLUMNS]


# ============================================================
# 列名容错
# ============================================================
@pytest.mark.parametrize(
    "raw,expect",
    [
        ("能量(kcal)", "energy_kcal"),
        ("能量（kcal）", "energy_kcal"),
        ("能量 kcal", "energy_kcal"),
        ("能量", "energy_kcal"),
        ("能量(kcal) *", "energy_kcal"),
        ("维生素C(mg)", "vitamin_c_mg"),
        ("维c", "vitamin_c_mg"),
        ("碳水化合物(g)", "carb_g"),
        ("碳水", "carb_g"),
        ("食物名称", "name"),
        ("来源", "source"),
    ],
)
def test_header_tolerance(raw, expect):
    col = importer.resolve_header(raw)
    assert col is not None, raw
    assert col.key == expect


def test_unknown_header_returns_none():
    assert importer.resolve_header("编外列") is None


# ============================================================
# 数值解析
# ============================================================
def test_number_with_matching_unit_ok():
    assert importer.parse_number("11.2g", "g").value == 11.2
    assert importer.parse_number("5.40μg", "μg").value == 5.4
    assert importer.parse_number("12 mg", "mg").value == 12.0


def test_number_unit_mismatch_is_error_not_conversion():
    r = importer.parse_number("11.2g", "mg")
    assert not r.ok
    assert "单位不符" in r.error
    # 关键：不能悄悄乘 1000
    assert r.value is None


def test_kj_treated_as_kcal_gets_specific_hint():
    r = importer.parse_number("1478kJ", "kcal")
    assert not r.ok
    assert "4.184" in r.error


def test_empty_tokens_mean_missing_not_zero():
    for token in ("", "—", "-", "/", "Tr", "ND", None):
        assert importer.parse_number(token, "mg").value is None


def test_thousands_separator():
    assert importer.parse_number("1,234", "mg").value == 1234.0


def test_garbage_number_is_error():
    assert not importer.parse_number("约 12", "mg").ok


# ============================================================
# 行级校验
# ============================================================
def test_clean_row_is_ok():
    r = importer.validate_row(2, _row(), _mapped(HEADERS))
    assert r.verdict == "ok", [m.text for m in r.messages]
    assert r.importable
    assert len(r.payload["nutrients"]) == 15


def test_missing_required_nutrient_is_error():
    r = importer.validate_row(2, _row(energy_kcal=None), _mapped(HEADERS))
    assert r.verdict == "error"
    assert any("能量" in m.text and "必填" in m.text for m in r.messages)
    assert not r.importable


def test_optional_nutrient_missing_is_fine():
    r = importer.validate_row(2, _row(calcium_mg=None), _mapped(HEADERS))
    assert r.verdict == "ok"
    assert "calcium_mg" not in r.payload["nutrients"]


def test_bad_category_is_error_with_options():
    r = importer.validate_row(2, _row(category="香草"), _mapped(HEADERS))
    assert r.verdict == "error"
    msg = " ".join(m.text for m in r.messages)
    assert "不在枚举内" in msg and "蔬菜" in msg


def test_category_suffix_is_tolerated():
    r = importer.validate_row(2, _row(category="蔬菜类"), _mapped(HEADERS))
    assert r.payload["category"] == "蔬菜"
    assert r.verdict == "ok"


def test_nature_variant_keeps_main_nature_with_note():
    # 「微温」不在枚举里，但主性明确，按「温」记录并留一条 info 说明
    r = importer.validate_row(2, _row(nature="微温"), _mapped(HEADERS))
    assert r.payload["nature"] == "温"
    assert any(m.level == "info" and "性味" in m.text for m in r.messages)


def test_unknown_nature_is_warning_and_dropped():
    r = importer.validate_row(2, _row(nature="看情况"), _mapped(HEADERS))
    assert r.verdict == "warn"
    assert r.payload["nature"] == ""
    assert r.importable


def test_negative_value_is_error():
    r = importer.validate_row(2, _row(calcium_mg=-3), _mapped(HEADERS))
    assert r.verdict == "error"


def test_energy_over_upper_bound_suggests_kj():
    r = importer.validate_row(2, _row(energy_kcal=1478), _mapped(HEADERS))
    assert r.verdict == "error"
    assert any("kJ" in m.text for m in r.messages)


def test_macro_sum_over_100_is_warning():
    r = importer.validate_row(
        2, _row(protein_g=60, fat_g=40, carb_g=30, energy_kcal=700), _mapped(HEADERS)
    )
    assert r.verdict == "warn"
    assert any("超过 100g" in m.text for m in r.messages)


def test_energy_vs_atwater_mismatch_is_warning():
    # 蛋白 14.1 / 脂肪 6.1 / 碳水 64.2 → 推算 368；写 60 差太远
    r = importer.validate_row(2, _row(energy_kcal=60), _mapped(HEADERS))
    assert any("推算" in m.text for m in r.messages)


def test_empty_name_is_error():
    r = importer.validate_row(2, _row(name=""), _mapped(HEADERS))
    assert r.verdict == "error"


def test_long_name_is_error():
    r = importer.validate_row(2, _row(name="很" * 41), _mapped(HEADERS))
    assert r.verdict == "error"


def test_name_without_source_warns_only():
    r = importer.validate_row(2, _row(source=""), _mapped(HEADERS))
    assert r.verdict == "warn"
    assert r.importable


def test_aliases_split_and_dedup():
    r = importer.validate_row(2, _row(aliases="南美藜；昆诺阿藜; 南美藜"), _mapped(HEADERS))
    assert r.payload["aliases"] == ["南美藜", "昆诺阿藜"]


def test_duplicate_in_file_only_keeps_first():
    t = importer.ParsedTable(HEADERS, _mapped(HEADERS), [(2, _row()), (3, _row())])
    results = importer.validate_table(t)
    assert results[0].verdict == "ok"
    assert results[1].verdict == "duplicate_file"
    assert not results[1].importable
    s = importer.summarize(results)
    assert s["importable"] == 1


# ============================================================
# 模板自检：生成的模板必须能被自己解析
# ============================================================
def test_generated_xlsx_roundtrip(tmp_path):
    from scripts.make_import_template import build_xlsx

    path = tmp_path / "t.xlsx"
    build_xlsx(path)
    table = importer.parse_table("t.xlsx", path.read_bytes())
    assert table.sheet == "食物导入"
    assert not table.unknown_headers
    results = importer.validate_table(table)
    assert len(results) == len(importer.sample_rows())
    assert all(r.verdict == "ok" for r in results), [
        (r.name, [m.text for m in r.messages]) for r in results
    ]


def test_csv_gbk_encoding_is_accepted():
    buf = io.StringIO()
    w = csv.writer(buf)
    w.writerow(HEADERS)
    w.writerow(_row())
    blob = buf.getvalue().encode("gb18030")
    table = importer.parse_table("t.csv", blob)
    results = importer.validate_table(table)
    assert results[0].verdict == "ok"


def test_xls_is_rejected_with_clear_message():
    with pytest.raises(importer.ImportError_) as exc:
        importer.parse_table("t.xls", b"\xd0\xcf\x11\xe0")
    assert "另存为" in str(exc.value)


def test_too_many_rows_is_rejected(monkeypatch):
    monkeypatch.setattr(importer, "MAX_ROWS", 2)
    buf = io.StringIO()
    w = csv.writer(buf)
    w.writerow(HEADERS)
    for i in range(4):
        w.writerow(_row(name=f"食物{i}"))
    with pytest.raises(importer.ImportError_) as exc:
        importer.parse_table("t.csv", buf.getvalue().encode())
    assert "最多导入" in str(exc.value)


def test_name_key_keeps_parentheses_content():
    # 「豆腐(北)」和「豆腐(南)」是两个食物，归一化不能把它们合并
    assert importer.name_key(" 豆腐 （北） ") == importer.name_key("豆腐(北)")
    assert importer.name_key("豆腐(北)") != importer.name_key("豆腐(南)")


# ------------------------------------------------------------------
# 与图谱营养素词表的一致性（这一条是「静默丢数据」的守门员）
# ------------------------------------------------------------------
def test_every_nutrient_column_resolves_to_its_graph_id():
    """模板里的每一列都必须能映射到图谱营养素节点，且 id 与列定义一致。

    历史事故：kg/nutrients.py 原来只认 USDA 那 6 项，
    导入钙 / 铁 / 锌 / 维生素C 全被 resolve() 判成 None，审核时静默跳过 ——
    用户填了 15 项，进图谱的只有 4 项，而页面上一切正常。
    这条用例把「列定义」和「图谱词表」钉在一起，任何一边漏了都会红。
    """
    from kg import nutrients

    missing = []
    for col in importer.NUTRIENT_COLUMNS:
        ref = nutrients.resolve(importer.NUTRIENT_LABELS[col.key], col.unit)
        if ref is None:
            missing.append(f"{col.header}（{importer.NUTRIENT_LABELS[col.key]} {col.unit}）")
            continue
        assert ref.canonical_id == col.nutrient_id, (
            f"{col.header} 映射到 {ref.canonical_id}，列定义写的是 {col.nutrient_id}"
        )
    assert not missing, "这些列在图谱词表里找不到对应节点：" + "、".join(missing)


def test_energy_kj_is_still_skipped():
    """kJ 不单独建边（图里只收 kcal），这条老规则不能被上面的改动破坏。"""
    from kg import nutrients

    assert nutrients.resolve("能量", "kJ") is None
    assert nutrients.resolve("能量", "kcal").canonical_id == "NUT_00021"


def test_nutrient_map_csv_matches_resolver():
    """crosswalk CSV 与 kg/nutrients.py 必须逐条对得上。

    两份东西各管一段：CSV 负责建节点和别名，nutrients.py 负责审核时把
    (中文名, 单位) 翻成 id。只改一处的后果是「节点建出来了，导入却被静默跳过」——
    页面上一切正常，图里就是没有那条边。2026-10-28 扩词表时差点就这么干。
    """
    import csv as _csv
    from pathlib import Path

    from kg import nutrients

    path = Path(__file__).resolve().parent.parent / "data" / "crosswalk" / "nutrient_map.csv"
    rows = list(_csv.DictReader(path.open(encoding="utf-8-sig")))
    assert rows, "nutrient_map.csv 是空的"

    bad = []
    for r in rows:
        ref = nutrients.resolve(r["name"], r["unit"])
        if ref is None:
            bad.append(f"{r['canonical_id']} {r['name']}（{r['unit']}）resolver 认不出")
        elif ref.canonical_id != r["canonical_id"]:
            bad.append(f"{r['name']} CSV 写 {r['canonical_id']}，resolver 给 {ref.canonical_id}")
    assert not bad, "；".join(bad)


def test_nutrient_ids_are_unique_per_column():
    """两列指向同一个营养素节点 = 同一条边被写两遍，后者覆盖前者。"""
    ids = [c.nutrient_id for c in importer.NUTRIENT_COLUMNS]
    dup = {i for i in ids if ids.count(i) > 1}
    assert not dup, f"重复的 NUT id：{dup}"


def test_fatty_acid_columns_accept_plain_percent():
    """脂肪酸列单位是 %E（占总能量百分比）。

    用户手填几乎都写「24」或「24%」，为此报「单位不符」没有意义 ——
    这里的折算是**同一个量的两种写法**，不是把 mg 当 g 的那种换算。
    """
    from kg.importer import parse_number

    assert parse_number("24", "%E").value == 24
    assert parse_number("24%", "%E").value == 24
    assert parse_number("24%E", "%E").value == 24
    # 但真正的单位错误照样要拦
    assert not parse_number("24g", "%E").ok


def test_new_nutrient_headers_are_recognised_by_synonyms():
    """成分表里写的是学名（硫胺素/核黄素/尼克酸），模板是商品名。"""
    for raw, key in (
        ("硫胺素(mg)", "vitamin_b1_mg"),
        ("核黄素", "vitamin_b2_mg"),
        ("尼克酸(mg)", "niacin_mg"),
        ("α-生育酚", "vitamin_e_mg"),
        ("总胆固醇(mg)", "cholesterol_mg"),
        ("磷", "phosphorus_mg"),
        ("饱和脂肪酸(占能量%)", "sfa_pct"),
    ):
        col = importer.resolve_header(raw)
        assert col is not None, f"表头「{raw}」认不出来"
        assert col.key == key, f"「{raw}」应该映射到 {key}，实际 {col.key}"


def test_sample_rows_align_with_columns():
    """示例行必须和列一一对齐。

    之前是按位置写的：加一列就得数一遍位置，错位不会报错，
    只会让模板里的示例值整列串到隔壁营养素上。
    """
    widths = {len(r) for r in importer.sample_rows()}
    assert widths == {len(importer.COLUMNS)}
    # 示例里必有名称与分类，且第一条是三列示例中唯一填了性味的
    first = dict(zip([c.key for c in importer.COLUMNS], importer.sample_rows()[0]))
    assert first["name"] and first["category"]
    assert first["energy_kcal"] == 368
