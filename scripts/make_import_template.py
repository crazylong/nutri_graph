"""
scripts/make_import_template.py —— 生成食物导入模板（xlsx + csv）

为什么模板要「生成」而不是手搓一个文件放在仓库里：
  模板列名、单位、枚举、示例值都定义在 kg/importer.py 的 COLUMNS 里。
  解析端按那份契约认列，模板却另存一份手写版本，两边一定会漂移 ——
  用户下的模板填完导不进去，是最难解释的一类 bug。
  所以：**契约一份，模板每次由它生成**。

用法：
  python -m scripts.make_import_template            # 写到 frontend/template/
  python -m scripts.make_import_template --out /tmp # 换个目录
"""
from __future__ import annotations

import argparse
import csv
import io
from pathlib import Path

from kg.importer import (
    CATEGORIES,
    COLUMNS,
    NATURES,
    TEMPLATE_VERSION,
    sample_rows,
)

ROOT = Path(__file__).resolve().parent.parent
DEFAULT_OUT = ROOT / "frontend" / "template"
BASENAME = f"food_import_{TEMPLATE_VERSION}"

HEADER_FILL = "2F7D5D"      # 与前端 --accent 一致
REQUIRED_FILL = "EAF3EE"
NOTE_FILL = "FDF4E6"


def _width(col) -> float:
    base = {
        "name": 16, "aliases": 24, "category": 10, "nature": 8,
        "english_name": 18, "scientific_name": 24, "source": 30, "note": 26,
    }
    if col.role == "field":
        return base.get(col.key, 16)
    return max(11, len(col.header) + 3)


def build_xlsx(path: Path) -> None:
    from openpyxl import Workbook
    from openpyxl.comments import Comment
    from openpyxl.styles import Alignment, Border, Font, PatternFill, Side
    from openpyxl.utils import get_column_letter
    from openpyxl.worksheet.datavalidation import DataValidation

    wb = Workbook()

    # ---------------- Sheet 1：数据页 ----------------
    ws = wb.active
    ws.title = "食物导入"

    thin = Side(style="thin", color="D8DED9")
    border = Border(left=thin, right=thin, top=thin, bottom=thin)

    for idx, col in enumerate(COLUMNS, start=1):
        cell = ws.cell(row=1, column=idx, value=col.header)
        cell.font = Font(bold=True, color="FFFFFF", size=10)
        cell.fill = PatternFill("solid", fgColor=HEADER_FILL)
        cell.alignment = Alignment(horizontal="center", vertical="center", wrap_text=True)
        cell.border = border
        # 必填列用 ★ 标在列名后，比只靠「字段说明」页显眼
        if col.required:
            cell.value = f"{col.header} *"
        if col.hint:
            cell.comment = Comment(f"{col.hint}\n（{'必填' if col.required else '可选'}）", "食物导入")
        ws.column_dimensions[get_column_letter(idx)].width = _width(col)
    ws.row_dimensions[1].height = 30

    for r, row in enumerate(sample_rows(), start=2):
        for c, value in enumerate(row, start=1):
            cell = ws.cell(row=r, column=c, value=value)
            cell.font = Font(size=10, color="5B675F" if r > 4 else "1C2420")
            cell.alignment = Alignment(
                horizontal="left" if COLUMNS[c - 1].role == "field" else "right",
                vertical="center",
            )
            cell.border = border
            if COLUMNS[c - 1].role == "nutrient":
                cell.number_format = "General"

    ws.freeze_panes = "B2"
    ws.auto_filter.ref = f"A1:{get_column_letter(len(COLUMNS))}{len(sample_rows()) + 1}"

    # 分类 / 性味下拉：用户最常错的就是这两列的自由发挥
    def _dv(items, col_letter: str, title: str) -> None:
        dv = DataValidation(
            type="list",
            formula1='"' + ",".join(items) + '"',
            allow_blank=True,
            showDropDown=False,
        )
        dv.error = f"请从下拉里选，不要自己写（可选：{'/'.join(items)}）"
        dv.errorTitle = title
        dv.prompt = f"可选：{'/'.join(items)}"
        dv.promptTitle = title
        dv.showErrorMessage = True
        dv.showInputMessage = True
        ws.add_data_validation(dv)
        dv.add(f"{col_letter}2:{col_letter}1000")

    cat_letter = get_column_letter([c.header for c in COLUMNS].index("分类") + 1)
    nat_letter = get_column_letter([c.header for c in COLUMNS].index("性味") + 1)
    _dv(CATEGORIES, cat_letter, "分类")
    _dv(NATURES, nat_letter, "性味")

    # ---------------- Sheet 2：填写说明 ----------------
    ws2 = wb.create_sheet("填写说明")
    ws2.column_dimensions["A"].width = 4
    ws2.column_dimensions["B"].width = 96

    lines = [
        ("h", "食物导入模板 v1 · 填写说明"),
        ("p", "一行一个食物，每 100 克可食部。填完保存为 .xlsx，直接在「食物导入」页上传即可。"),
        ("s", "必填"),
        ("l", "名称、分类、能量(kcal)、蛋白质(g)、脂肪(g)、碳水化合物(g)、来源 —— 带 * 的七列。"),
        ("l", "营养素至少 4 项；四项必填营养素已满足这个要求，其余按手上资料能填多少填多少。"),
        ("s", "单位不要换"),
        ("l", "能量必须是 kcal。如果你手上的资料是 kJ（中国食物成分表常用 kJ），先除以 4.184 再填。"),
        ("l", "系统不做单位换算：mg 写成 g 会被直接判错，因为 1000 倍的误差比报错更难发现。"),
        ("s", "可以留空"),
        ("l", "任何可选营养素留空都正常 —— 原表里没有这一项，比填 0 更准确（0 表示「检出为零」）。"),
        ("l", "性味、英文名、学名、备注都可以空。"),
        ("s", "重复导入"),
        ("l", "同一名称再次导入会覆盖旧记录（按名称去重），不会产生两条。"),
        ("l", "名称里带括号的算不同食物：「豆腐(北)」和「豆腐(南)」是两条。"),
        ("s", "上限"),
        ("l", "单次最多 500 行，文件不超过 1MB（纯数据表通常只有几十 KB）。"),
        ("l", "表里三行是示例（备注列写着「示例：…」），填之前请删掉或直接覆盖。"),
        ("s", "数据说明"),
        ("l", "上传后进入补录审核队列，审核通过才会写进知识图谱并参与问答；未通过前不影响线上回答。"),
        ("l", "营养素列已覆盖中国食物成分表的常见项目（含磷、硒、铜、锰、碘、胆固醇、B 族维生素、"),
        ("l", "维生素 E 和三种脂肪酸）。三种脂肪酸的单位是「占总能量百分比」，不是克。"),
        ("l", "请只导入你有权使用的数据。中国食物成分表版权归中国疾控中心营养与健康所，"),
        ("l", "商用需另行取得授权；演示场景请标注来源。"),
    ]
    r = 1
    for kind, text in lines:
        cell = ws2.cell(row=r, column=2, value=text)
        if kind == "h":
            cell.font = Font(bold=True, size=15, color="1C2420")
            ws2.row_dimensions[r].height = 30
        elif kind == "s":
            cell.font = Font(bold=True, size=11, color=HEADER_FILL)
            ws2.row_dimensions[r].height = 24
        else:
            cell.font = Font(size=10.5, color="3C463F")
            cell.alignment = Alignment(wrap_text=True, vertical="top")
            ws2.row_dimensions[r].height = 18 if kind == "p" else 17
        r += 1

    # ---------------- Sheet 3：字段说明 ----------------
    ws3 = wb.create_sheet("字段说明")
    heads = ["列名", "必填", "单位", "图谱营养素 id", "合理上限", "说明"]
    widths = [20, 8, 10, 16, 12, 60]
    for i, (h, w) in enumerate(zip(heads, widths), start=1):
        cell = ws3.cell(row=1, column=i, value=h)
        cell.font = Font(bold=True, color="FFFFFF", size=10)
        cell.fill = PatternFill("solid", fgColor=HEADER_FILL)
        cell.alignment = Alignment(horizontal="center", vertical="center")
        ws3.column_dimensions[get_column_letter(i)].width = w
    for r, col in enumerate(COLUMNS, start=2):
        vals = [
            col.header,
            "是" if col.required else "",
            col.unit or "",
            col.nutrient_id or "",
            f"{col.upper:g}" if col.upper else "",
            col.hint or ("每 100 克可食部" if col.role == "nutrient" else ""),
        ]
        for c, v in enumerate(vals, start=1):
            cell = ws3.cell(row=r, column=c, value=v)
            cell.font = Font(size=10, color="3C463F")
            if c == 2 and col.required:
                cell.fill = PatternFill("solid", fgColor=REQUIRED_FILL)
        ws3.row_dimensions[r].height = 17
    ws3.freeze_panes = "A2"

    wb.save(path)


def build_csv(path: Path) -> None:
    buf = io.StringIO()
    w = csv.writer(buf, lineterminator="\n")
    w.writerow([c.header for c in COLUMNS])
    for row in sample_rows():
        w.writerow(row)
    # UTF-8 BOM：不加的话 Excel 双击打开中文全是乱码
    path.write_text(buf.getvalue(), encoding="utf-8-sig")


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--out", default=str(DEFAULT_OUT), help="输出目录")
    args = ap.parse_args()

    out = Path(args.out)
    out.mkdir(parents=True, exist_ok=True)
    xlsx, csvp = out / f"{BASENAME}.xlsx", out / f"{BASENAME}.csv"
    build_xlsx(xlsx)
    build_csv(csvp)
    print(f"✅ {xlsx}  ({xlsx.stat().st_size // 1024} KB)")
    print(f"✅ {csvp}   ({csvp.stat().st_size // 1024} KB)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
