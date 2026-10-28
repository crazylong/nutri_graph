"""
kg/importer.py —— 食物表格导入的解析与行级校验

设计原则（都是从 admin_plan_v2 §6 落的）：
  1. **契约先行**：模板列名、单位、枚举都在这里定义一次，模板生成、解析、校验、
     前端字段说明全部读同一份 `COLUMNS`。三处各写一遍必然漂移。
  2. **不做单位自动换算**。mg 写成 g 是 1000 倍的事故，自动猜比报错更危险；
     kJ 写成 kcal 同理，只给提示不改数。
  3. **行级 verdict 而不是整批失败**。一张表里错三行，不应该让另外两百行白干。
  4. 校验只做「能进的进、不能进的进不了」，**不写库**。写库在 api/food_import.py，
     这样同一份解析能给预览和提交复用，预览绝不会产生脏数据。

与图谱的关系：这里产出的 `nutrients` 用**稳定英文 key**，并带 `nid`
（NUT_00006 这类图谱营养素 id），将来接 kg_food_bundle 时不用再猜一遍映射。
"""
from __future__ import annotations

import csv
import io
import logging
import re
import unicodedata
from dataclasses import dataclass, field
from typing import Any, Literal

log = logging.getLogger("kg.importer")

TEMPLATE_VERSION = "v1"

# ============================================================
# 枚举（与 admin_plan_v2 §5.3 / §6.1 一致）
# ============================================================
CATEGORIES: tuple[str, ...] = (
    "蔬菜", "水果", "谷物", "豆类", "坚果",
    "肉类", "禽类", "水产", "蛋类", "奶类",
    "菌藻", "油脂", "调味", "其他",
)
NATURES: tuple[str, ...] = ("寒", "凉", "平", "温", "热")

# 名称里出现这些字符基本可以确定不是食材（纯符号行、表头重复行）
_NON_NAME_CHARS = re.compile(r"^[\W_]+$", re.UNICODE)


# ============================================================
# 列契约
# ============================================================
@dataclass(frozen=True)
class Column:
    """一列的定义。header 是模板里的中文列名，也是解析时的匹配依据。"""

    header: str
    key: str
    role: Literal["field", "nutrient"]
    required: bool = False
    unit: str | None = None
    nutrient_id: str | None = None
    upper: float | None = None      # 合理上限，超了判「疑似单位错误」
    max_len: int = 0
    hint: str = ""


COLUMNS: tuple[Column, ...] = (
    # ---------- 基本字段 ----------
    Column("名称", "name", "field", required=True, max_len=40,
           hint="规范名，1–40 字。同一食物重复导入会覆盖旧数据"),
    Column("别名", "aliases", "field", max_len=200,
           hint="多个用英文分号 ; 分隔，最多 10 个"),
    Column("分类", "category", "field", required=True,
           hint="固定枚举，见「字段说明」页下拉"),
    Column("性味", "nature", "field",
           hint="寒/凉/平/温/热，可空"),
    Column("英文名", "english_name", "field", max_len=80),
    Column("学名", "scientific_name", "field", max_len=120),
    # ---------- 营养素（每 100g 可食部）----------
    Column("能量(kcal)", "energy_kcal", "nutrient", required=True,
           unit="kcal", nutrient_id="NUT_00021", upper=900,
           hint="每 100 克。注意是 kcal，不是 kJ"),
    Column("蛋白质(g)", "protein_g", "nutrient", required=True,
           unit="g", nutrient_id="NUT_00012", upper=100),
    Column("脂肪(g)", "fat_g", "nutrient", required=True,
           unit="g", nutrient_id="NUT_00013", upper=100),
    Column("碳水化合物(g)", "carb_g", "nutrient", required=True,
           unit="g", nutrient_id="NUT_00014", upper=100),
    Column("膳食纤维(g)", "fiber_g", "nutrient",
           unit="g", nutrient_id="NUT_00011", upper=100),
    Column("钠(mg)", "sodium_mg", "nutrient",
           unit="mg", nutrient_id="NUT_00022", upper=40000),
    Column("钙(mg)", "calcium_mg", "nutrient",
           unit="mg", nutrient_id="NUT_00006", upper=5000),
    Column("铁(mg)", "iron_mg", "nutrient",
           unit="mg", nutrient_id="NUT_00007", upper=400,
           hint="干制菌藻/干菜可到两百多（苔菜干 283.7），不是错"),
    Column("锌(mg)", "zinc_mg", "nutrient",
           unit="mg", nutrient_id="NUT_00008", upper=100),
    Column("钾(mg)", "potassium_mg", "nutrient",
           unit="mg", nutrient_id="NUT_00009", upper=15000,
           hint="干制菌藻/干菜可到万字级（竹荪干 11882），不是错"),
    Column("镁(mg)", "magnesium_mg", "nutrient",
           unit="mg", nutrient_id="NUT_00010", upper=1500),
    Column("维生素C(mg)", "vitamin_c_mg", "nutrient",
           unit="mg", nutrient_id="NUT_00001", upper=3000),
    Column("维生素A(μgRAE)", "vitamin_a_ug", "nutrient",
           unit="μgRAE", nutrient_id="NUT_00002", upper=30000,
           hint="上限按肝脏类定：羊肝 20972μgRAE/100g 是真实值"),
    Column("胡萝卜素(μg)", "carotene_ug", "nutrient",
           unit="μg", nutrient_id="NUT_00015", upper=30000),
    # ---------- 第二批（2026-10-28）：中国食物成分表里图谱原本缺的那几项 ----------
    # 上限都按真实数据留了余量（注释里是实测最大值），不是拍脑袋：
    # 定太窄会把干制品/内脏/海藻整批判成「疑似单位错误」，那些恰恰是营养最密的食材。
    Column("磷(mg)", "phosphorus_mg", "nutrient",
           unit="mg", nutrient_id="NUT_00023", upper=3000,
           hint="酵母(干) 1893mg/100g 是真实值"),
    Column("硒(μg)", "selenium_ug", "nutrient",
           unit="μg", nutrient_id="NUT_00026", upper=2000,
           hint="菌菇干品可到 758μg（牛肝菌干），不是错"),
    Column("铜(mg)", "copper_mg", "nutrient",
           unit="mg", nutrient_id="NUT_00027", upper=50,
           hint="酵母 20mg/100g 是真实值"),
    Column("锰(mg)", "manganese_mg", "nutrient",
           unit="mg", nutrient_id="NUT_00029", upper=200,
           hint="河蚌 59.6mg/100g 是真实值"),
    Column("碘(μg)", "iodine_ug", "nutrient",
           unit="μg", nutrient_id="NUT_00031", upper=30000,
           hint="干海藻万字级（裙带菜干 15878μg），不是错"),
    Column("胆固醇(mg)", "cholesterol_mg", "nutrient",
           unit="mg", nutrient_id="NUT_00028", upper=5000,
           hint="蛋黄粉 2850mg/100g；植物性食物原表为空，留空即可"),
    Column("维生素B1(mg)", "vitamin_b1_mg", "nutrient",
           unit="mg", nutrient_id="NUT_00003", upper=20,
           hint="酵母 6.56mg/100g 是真实值"),
    Column("维生素B2(mg)", "vitamin_b2_mg", "nutrient",
           unit="mg", nutrient_id="NUT_00024", upper=20,
           hint="又名核黄素；干菇可到 6.9mg"),
    Column("烟酸(mg)", "niacin_mg", "nutrient",
           unit="mg", nutrient_id="NUT_00025", upper=200,
           hint="又名尼克酸/维生素B3；干菌菇可到 67.7mg"),
    Column("维生素E(mg)", "vitamin_e_mg", "nutrient",
           unit="mg", nutrient_id="NUT_00030", upper=200,
           hint="按 α-生育酚计；芝麻油 67.4mg/100g 是真实值"),
    Column("饱和脂肪酸(占能量%)", "sfa_pct", "nutrient",
           unit="%E", nutrient_id="NUT_00032", upper=110,
           hint="占总能量百分比，不是克。写 24 或 24% 都认"),
    Column("单不饱和脂肪酸(占能量%)", "mufa_pct", "nutrient",
           unit="%E", nutrient_id="NUT_00033", upper=110,
           hint="占总能量百分比，不是克"),
    Column("多不饱和脂肪酸(占能量%)", "pufa_pct", "nutrient",
           unit="%E", nutrient_id="NUT_00034", upper=110,
           hint="占总能量百分比，不是克"),
    Column("嘌呤(mg)", "purine_mg", "nutrient",
           unit="mg", nutrient_id="NUT_00019", upper=1000),
    # ---------- 溯源 ----------
    Column("来源", "source", "field", required=True, max_len=200,
           hint="书名/版本/页码，或 URL。整行共用"),
    Column("备注", "note", "field", max_len=200,
           hint="不进图谱，只留档"),
)

REQUIRED_NUTRIENT_KEYS = tuple(c.key for c in COLUMNS if c.role == "nutrient" and c.required)
NUTRIENT_COLUMNS = tuple(c for c in COLUMNS if c.role == "nutrient")

# 营养素展示顺序（前端卡片按这个排）
NUTRIENT_LABELS: dict[str, str] = {
    "energy_kcal": "能量",
    "protein_g": "蛋白质",
    "fat_g": "脂肪",
    "carb_g": "碳水化合物",
    "fiber_g": "膳食纤维",
    "sodium_mg": "钠",
    "calcium_mg": "钙",
    "iron_mg": "铁",
    "zinc_mg": "锌",
    "potassium_mg": "钾",
    "magnesium_mg": "镁",
    "vitamin_c_mg": "维生素C",
    "vitamin_a_ug": "维生素A",
    "carotene_ug": "胡萝卜素",
    "phosphorus_mg": "磷",
    "selenium_ug": "硒",
    "copper_mg": "铜",
    "manganese_mg": "锰",
    "iodine_ug": "碘",
    "cholesterol_mg": "胆固醇",
    "vitamin_b1_mg": "维生素B1",
    "vitamin_b2_mg": "维生素B2",
    "niacin_mg": "烟酸",
    "vitamin_e_mg": "维生素E",
    "sfa_pct": "饱和脂肪酸",
    "mufa_pct": "单不饱和脂肪酸",
    "pufa_pct": "多不饱和脂肪酸",
    "purine_mg": "嘌呤",
}


# ============================================================
# 列名匹配：容错但不猜
# ============================================================
_WS = re.compile(r"\s+")
_PAREN = str.maketrans({"（": "(", "）": ")", "，": ",", "：": ":", "／": "/"})


def _norm_header(s: str) -> str:
    """把列名压成可比较形式：全半角统一、去空白、去括号内单位、小写。

    「能量(kcal)」「能量（kcal）」「能量 kcal」「能量」都当成同一列 ——
    用户从别的表格复制过来时单位括注经常变，为此报错没有意义。
    """
    s = unicodedata.normalize("NFKC", str(s or "")).translate(_PAREN)
    s = _WS.sub("", s)
    s = s.replace("（", "(").replace("）", ")")
    s = re.sub(r"\(.*?\)", "", s)      # 去掉括号及里面
    # 「能量 kcal」这种把单位写在括号外的也要认出来
    s = re.sub(r"(kcal|kj|g|mg|ug|μg|rae|%)$", "", s)
    s = s.rstrip("*＊")                # 必填列在模板里带 *，不算列名的一部分
    return s.strip().lower()


_HEADER_INDEX: dict[str, Column] = {}
for _c in COLUMNS:
    _HEADER_INDEX[_norm_header(_c.header)] = _c

# 额外接受的写法（只加「同义」，不加「换算」）
_HEADER_ALIASES: dict[str, str] = {
    "食物名称": "名称",
    "食材名称": "名称",
    "食物": "名称",
    "英文": "英文名",
    "拉丁名": "学名",
    "食物分类": "分类",
    "类别": "分类",
    "性": "性味",
    "能量": "能量(kcal)",
    "热量": "能量(kcal)",
    "卡路里": "能量(kcal)",
    "蛋白质": "蛋白质(g)",
    "脂肪": "脂肪(g)",
    "碳水": "碳水化合物(g)",
    "碳水化合物": "碳水化合物(g)",
    "糖类": "碳水化合物(g)",
    "膳食纤维": "膳食纤维(g)",
    "纤维": "膳食纤维(g)",
    "钠": "钠(mg)",
    "钙": "钙(mg)",
    "铁": "铁(mg)",
    "锌": "锌(mg)",
    "钾": "钾(mg)",
    "镁": "镁(mg)",
    "维生素c": "维生素C(mg)",
    "维c": "维生素C(mg)",
    "维生素a": "维生素A(μgRAE)",
    "胡萝卜素": "胡萝卜素(μg)",
    "嘌呤": "嘌呤(mg)",
    # 第二批营养素：用户手上的表常写成分表里的学名（硫胺素/核黄素/尼克酸）
    "硫胺素": "维生素B1(mg)",
    "b1": "维生素B1(mg)",
    "维生素b1": "维生素B1(mg)",
    "核黄素": "维生素B2(mg)",
    "b2": "维生素B2(mg)",
    "维生素b2": "维生素B2(mg)",
    "尼克酸": "烟酸(mg)",
    "维生素b3": "烟酸(mg)",
    "b3": "烟酸(mg)",
    "生育酚": "维生素E(mg)",
    "α-生育酚": "维生素E(mg)",
    "α生育酚": "维生素E(mg)",
    "总胆固醇": "胆固醇(mg)",
    "数据来源": "来源",
    "出处": "来源",
    "参考资料": "来源",
    "说明": "备注",
}
for _alias, _target in _HEADER_ALIASES.items():
    _HEADER_INDEX.setdefault(_norm_header(_alias), _HEADER_INDEX[_norm_header(_target)])


def resolve_header(raw: str) -> Column | None:
    return _HEADER_INDEX.get(_norm_header(raw))


# ============================================================
# 数值解析
# ============================================================
_EMPTY_TOKENS = {"", "—", "-", "－", "–", "/", "n/a", "na", "nd", "tr", "trace", "null", "none", "无", "未测", "未检出"}
_NUM_RE = re.compile(r"^([+-]?\d+(?:[.,]\d+)?)\s*([a-zA-Zμµ%/]*)\s*$")

_UNIT_FOLD = {
    "kcal": "kcal", "千卡": "kcal", "大卡": "kcal", "kal": "kcal",
    "kj": "kJ", "千焦": "kJ",
    "g": "g", "克": "g", "gram": "g",
    "mg": "mg", "毫克": "mg",
    "ug": "μg", "μg": "μg", "µg": "μg", "mcg": "μg", "微克": "μg",
    "μgrae": "μgRAE", "ugrae": "μgRAE", "µgrae": "μgRAE",
    "%": "%",
    # 脂肪酸三项的单位是「占总能量百分比」，列名写成 %E。用户手填时通常写 24 或 24%，
    # 都折到 %E；反过来 %E 也不会被当成毫克。
    "%e": "%E",
    "占能量%": "%E",
    "%能量": "%E",
}


def _fold_unit(u: str) -> str:
    return _UNIT_FOLD.get(unicodedata.normalize("NFKC", u).strip().lower(), u.strip())


@dataclass
class NumParse:
    value: float | None = None      # None = 空
    error: str = ""

    @property
    def ok(self) -> bool:
        return not self.error


def parse_number(raw: Any, unit: str | None) -> NumParse:
    """把单元格解析成数字。允许带单位后缀，但**单位不符直接报错，不换算**。"""
    if raw is None:
        return NumParse()
    if isinstance(raw, (int, float)) and not isinstance(raw, bool):
        return NumParse(value=float(raw))
    s = str(raw).strip()
    if s.lower() in _EMPTY_TOKENS:
        return NumParse()
    m = _NUM_RE.match(s.replace(" ", ""))
    if not m:
        return NumParse(error=f"「{s}」不是数字")
    # 千分位写成 1,234 视为 1234；写成 12,3（小数点误用）也能纠正，但 1,234.5 优先按千分位
    num_txt, suffix = m.group(1), m.group(2)
    if "," in num_txt:
        num_txt = num_txt.replace(",", "") if re.match(r"^\d{1,3}(,\d{3})+", num_txt) else num_txt.replace(",", ".")
    try:
        value = float(num_txt)
    except ValueError:
        return NumParse(error=f"「{s}」不是数字")

    if suffix and unit:
        got = _fold_unit(suffix)
        want = _fold_unit(unit)
        # 脂肪酸的 %E（占总能量百分比）允许省略 E 写成 24%，语义不会因此改变；
        # 但反过来把 g 当 mg 依然判错 —— 只有这一处是「同一个量的两种写法」。
        if want == "%E" and got == "%":
            got = "%E"
        if got and got != want:
            # 唯一值得给提示的换算错误：kJ 当 kcal
            if got == "kJ" and want == "kcal":
                return NumParse(
                    error=f"单元格写的是 kJ（{s}），本列要求 kcal；请先除以 4.184 再填"
                )
            return NumParse(
                error=f"单位不符：本列要求 {unit}，单元格写了 {suffix}（不做自动换算）"
            )
    return NumParse(value=value)


# ============================================================
# 表格读取
# ============================================================
class ImportError_(Exception):
    """文件级错误（打不开、没有表头、格式不支持）。行级错误不用异常。"""


@dataclass
class ParsedTable:
    headers: list[str]            # 原始表头
    mapped: list[Column | None]   # 每一列对应的契约（None=未识别的列，忽略）
    rows: list[tuple[int, list[Any]]]  # (表格行号, 单元格值)
    sheet: str = ""
    unknown_headers: list[str] = field(default_factory=list)


MAX_FILE_BYTES = 1024 * 1024
MAX_ROWS = 500

_SHEET_NAMES = ("食物导入", "食物", "导入", "Sheet1", "foods")


def _read_xlsx(blob: bytes) -> ParsedTable:
    try:
        from openpyxl import load_workbook
    except ImportError as exc:  # pragma: no cover - 部署缺依赖时的明确提示
        raise ImportError_("服务器缺少 openpyxl，无法解析 xlsx") from exc

    try:
        wb = load_workbook(io.BytesIO(blob), read_only=True, data_only=True)
    except Exception as exc:  # noqa: BLE001 - 损坏的 xlsx 各种异常都有
        raise ImportError_(f"这个文件打不开，可能已损坏或不是真正的 xlsx（{type(exc).__name__}）") from exc

    ws = None
    for name in _SHEET_NAMES:
        if name in wb.sheetnames:
            ws = wb[name]
            break
    if ws is None:
        ws = wb[wb.sheetnames[0]]

    it = ws.iter_rows(values_only=True)
    header_row = None
    header_no = 0
    for i, row in enumerate(it, start=1):
        cells = [c for c in row if c not in (None, "")]
        if len(cells) >= 3:            # 跳过标题/空行，找真正有 3 个以上表头的行
            header_row = list(row)
            header_no = i
            break
    if not header_row:
        raise ImportError_("表里没找到表头行（至少要有一行包含「名称」「分类」等列名）")

    headers = [str(c).strip() if c is not None else "" for c in header_row]
    mapped = [resolve_header(h) if h else None for h in headers]
    unknown = [h for h, m in zip(headers, mapped) if h and m is None]

    rows: list[tuple[int, list[Any]]] = []
    for i, row in enumerate(it, start=header_no + 1):
        vals = list(row[: len(headers)])
        if len(vals) < len(headers):
            vals += [None] * (len(headers) - len(vals))
        if all(v in (None, "") for v in vals):
            continue
        rows.append((i, vals))
        if len(rows) > MAX_ROWS:
            raise ImportError_(f"单次最多导入 {MAX_ROWS} 行，这个文件超了")
    wb.close()
    return ParsedTable(headers, mapped, rows, ws.title, unknown)


def _read_csv(blob: bytes) -> ParsedTable:
    text = None
    for enc in ("utf-8-sig", "utf-8", "gb18030"):
        try:
            text = blob.decode(enc)
            break
        except UnicodeDecodeError:
            continue
    if text is None:
        raise ImportError_("CSV 编码无法识别，请用 UTF-8 或 GBK 另存后重试")

    # Excel 导出的 CSV 常带 BOM 与逗号/制表符混用，先嗅探分隔符
    sample = text[:4096]
    delim = ","
    if sample.count("\t") > sample.count(","):
        delim = "\t"
    reader = csv.reader(io.StringIO(text), delimiter=delim)
    all_rows = [r for r in reader]
    if not all_rows:
        raise ImportError_("CSV 是空的")

    header_no = 0
    headers: list[str] = []
    for i, row in enumerate(all_rows):
        cells = [c for c in row if c.strip()]
        if len(cells) >= 3:
            headers = [c.strip() for c in row]
            header_no = i
            break
    if not headers:
        raise ImportError_("表里没找到表头行")

    mapped = [resolve_header(h) if h else None for h in headers]
    unknown = [h for h, m in zip(headers, mapped) if h and m is None]

    rows: list[tuple[int, list[Any]]] = []
    for i, row in enumerate(all_rows[header_no + 1:], start=header_no + 2):
        vals = list(row[: len(headers)])
        if len(vals) < len(headers):
            vals += [""] * (len(headers) - len(vals))
        if all(str(v).strip() == "" for v in vals):
            continue
        rows.append((i, vals))
        if len(rows) > MAX_ROWS:
            raise ImportError_(f"单次最多导入 {MAX_ROWS} 行，这个文件超了")
    return ParsedTable(headers, mapped, rows, "", unknown)


def parse_table(filename: str, blob: bytes) -> ParsedTable:
    if not blob:
        raise ImportError_("文件是空的")
    if len(blob) > MAX_FILE_BYTES:
        raise ImportError_(f"文件超过 {MAX_FILE_BYTES // 1024}KB 上限")
    name = (filename or "").lower()
    if name.endswith((".xlsx", ".xlsm")):
        table = _read_xlsx(blob)
    elif name.endswith(".csv"):
        table = _read_csv(blob)
    elif name.endswith(".xls"):
        raise ImportError_("不支持老版 .xls，请在 Excel 里「另存为 .xlsx」后重试")
    else:
        # 没有扩展名时按魔数判断（xlsx 是 zip，前两字节 PK）
        table = _read_xlsx(blob) if blob[:2] == b"PK" else _read_csv(blob)

    if not any(table.mapped):
        raise ImportError_("表头一个都没认出来。请下载模板后照着列名填，至少要有「名称」「分类」")
    return table


# ============================================================
# 行级校验
# ============================================================
@dataclass
class Message:
    level: Literal["error", "warn", "info"]
    text: str
    field: str = ""

    def as_dict(self) -> dict:
        return {"level": self.level, "text": self.text, "field": self.field}


@dataclass
class RowResult:
    row_no: int
    name: str
    verdict: Literal["ok", "warn", "error", "duplicate_file", "duplicate_db"]
    messages: list[Message] = field(default_factory=list)
    payload: dict[str, Any] = field(default_factory=dict)

    @property
    def importable(self) -> bool:
        return self.verdict in ("ok", "warn", "duplicate_db") and not any(
            m.level == "error" for m in self.messages
        )

    def as_dict(self) -> dict:
        return {
            "row_no": self.row_no,
            "name": self.name,
            "verdict": self.verdict,
            "importable": self.importable,
            "messages": [m.as_dict() for m in self.messages],
            "payload": self.payload,
        }

    def as_preview(self) -> dict:
        """预览用的精简版：不带完整的 payload。

        500 行 × 15 项营养素的完整 payload 有几百 KB，而预览表只显示
        名称/分类/能量/判定 —— 传全量纯属浪费带宽，还拖慢首屏。
        """
        n = self.payload.get("nutrients") or {}
        return {
            "row_no": self.row_no,
            "name": self.name,
            "aliases": (self.payload.get("aliases") or [])[:3],
            "category": self.payload.get("category", ""),
            "verdict": self.verdict,
            "importable": self.importable,
            "messages": [m.as_dict() for m in self.messages],
            # 只给数值，键是营养素 key；前端按 units 表格式化成「12.3 mg」
            "nutrients": {k: v["v"] for k, v in n.items()},
            "nutrient_count": len(n),
        }


def name_key(name: str) -> str:
    """归一化名，作为 demo_food 的唯一键。

    只做「宽度/空白/大小写」归一，**不动括号内容**：
    「豆腐(北)」和「豆腐(南)」是两个东西，把括号剥掉会合成一条。
    """
    s = unicodedata.normalize("NFKC", str(name or ""))
    s = _WS.sub("", s)
    return s.lower()


def _clean_text(v: Any) -> str:
    if v is None:
        return ""
    if isinstance(v, float) and v.is_integer():
        v = int(v)
    return unicodedata.normalize("NFKC", str(v)).strip()


def split_aliases(raw: Any) -> list[str]:
    s = _clean_text(raw)
    if not s:
        return []
    parts = re.split(r"[;；,，/|、]", s)
    out: list[str] = []
    for p in parts:
        p = p.strip()
        if p and p not in out:
            out.append(p)
    return out


def _normalize_category(raw: str) -> str:
    s = raw.strip()
    if s in CATEGORIES:
        return s
    # 「蔬菜类」「谷类」这类写法降级匹配
    for cand in (s.rstrip("类"), s.replace("类", "")):
        if cand in CATEGORIES:
            return cand
    alias = {
        "主食": "谷物", "粮食": "谷物", "谷薯": "谷物", "薯类": "谷物",
        "豆制品": "豆类", "干豆": "豆类",
        "坚果种子": "坚果", "种子": "坚果",
        "畜肉": "肉类", "红肉": "肉类",
        "鸡": "禽类", "鸭": "禽类", "蛋": "蛋类", "奶": "奶类", "乳品": "奶类",
        "鱼": "水产", "海鲜": "水产", "虾": "水产", "贝类": "水产",
        "菌类": "菌藻", "藻类": "菌藻", "蘑菇": "菌藻",
        "油": "油脂", "调料": "调味", "调味品": "调味",
    }
    return alias.get(s, s)


def _normalize_nature(raw: str) -> str:
    s = raw.strip()
    if not s:
        return ""
    if s in NATURES:
        return s
    # 「性平」「微温」「大寒」这类说法保留主性
    for n in NATURES:
        if n in s:
            return n
    return ""


def validate_row(
    row_no: int,
    row: list[Any],
    mapped: list[Column | None],
    *,
    relax_required: bool = False,
) -> RowResult:
    """校验一行，返回 verdict + 行级消息 + 规整后的 payload。

    relax_required：把「必填营养列为空」从 error 降为 warn。
    只给**机构测定的权威数据**用（scripts/import_fcd.py）：原表里空着就是
    「未测定」，不是用户漏填。人填的表一律保持严格 —— 漏一列通常是真的漏了。
    「至少 4 项营养素」这道闸门仍然生效，空行进不来。
    """
    raw: dict[str, Any] = {}
    for col, val in zip(mapped, row):
        if col is None:
            continue
        # 同名列只取第一次出现的（用户可能复制了两列）
        raw.setdefault(col.key, val)

    msgs: list[Message] = []
    payload: dict[str, Any] = {
        "name": "",
        "name_norm": "",
        "aliases": [],
        "category": "",
        "nature": "",
        "english_name": "",
        "scientific_name": "",
        "source": "",
        "note": "",
        "nutrients": {},
    }

    # ---------- 名称 ----------
    name = _clean_text(raw.get("name"))
    payload["name"] = name
    payload["name_norm"] = name_key(name)
    if not name:
        msgs.append(Message("error", "名称为空", "名称"))
    elif len(name) > 40:
        msgs.append(Message("error", f"名称 {len(name)} 字，超过 40 字上限", "名称"))
    elif _NON_NAME_CHARS.match(name):
        msgs.append(Message("error", f"「{name}」不是有效的食物名", "名称"))
    else:
        from kg.request import _NON_FOOD_TERMS  # 复用现有词表，别维护两份

        if name in _NON_FOOD_TERMS:
            msgs.append(Message("warn", f"「{name}」看着像营养素或泛称，确认是食物再导入", "名称"))

    # ---------- 别名 ----------
    aliases = split_aliases(raw.get("aliases"))
    if len(aliases) > 10:
        msgs.append(Message("warn", f"别名 {len(aliases)} 个，只保留前 10 个", "别名"))
        aliases = aliases[:10]
    for a in aliases:
        if len(a) > 40:
            msgs.append(Message("error", f"别名「{a[:12]}…」超过 40 字", "别名"))
    payload["aliases"] = aliases

    # ---------- 分类 ----------
    cat_raw = _clean_text(raw.get("category"))
    cat = _normalize_category(cat_raw) if cat_raw else ""
    if not cat_raw:
        msgs.append(Message("error", "分类为空", "分类"))
    elif cat not in CATEGORIES:
        msgs.append(
            Message("error", f"分类「{cat_raw}」不在枚举内，可选：{'/'.join(CATEGORIES)}", "分类")
        )
    payload["category"] = cat if cat in CATEGORIES else ""

    # ---------- 性味（可选）----------
    nat_raw = _clean_text(raw.get("nature"))
    if nat_raw:
        nat = _normalize_nature(nat_raw)
        if nat:
            payload["nature"] = nat
            if nat != nat_raw:
                msgs.append(Message("info", f"性味「{nat_raw}」按「{nat}」记录", "性味"))
        else:
            msgs.append(
                Message("warn", f"性味「{nat_raw}」不在枚举内，已忽略（可选：{'/'.join(NATURES)}）", "性味")
            )

    # ---------- 文本字段 ----------
    for key, header, limit in (
        ("english_name", "英文名", 80),
        ("scientific_name", "学名", 120),
        ("source", "来源", 200),
        ("note", "备注", 200),
    ):
        v = _clean_text(raw.get(key))
        if len(v) > limit:
            msgs.append(Message("warn", f"{header}超过 {limit} 字，已截断", header))
            v = v[:limit]
        payload[key] = v

    if not payload["source"]:
        msgs.append(Message("warn", "没填「来源」，这批数据以后无法追溯出处", "来源"))

    # ---------- 营养素 ----------
    for col in NUTRIENT_COLUMNS:
        parsed = parse_number(raw.get(col.key), col.unit)
        if not parsed.ok:
            msgs.append(Message("error", parsed.error, col.header))
            continue
        if parsed.value is None:
            if col.required:
                if relax_required:
                    msgs.append(
                        Message("warn", f"{col.header}原表为空，按「未测定」处理", col.header)
                    )
                else:
                    msgs.append(Message("error", f"{col.header}为空（必填）", col.header))
            continue
        v = parsed.value
        if v < 0:
            msgs.append(Message("error", f"{col.header} 是负数（{v}）", col.header))
            continue
        if col.upper is not None and v > col.upper:
            hint = ""
            if col.key == "energy_kcal" and v <= 4000:
                hint = "，看起来填的是 kJ（÷4.184 才是 kcal）"
            msgs.append(
                Message("error", f"{col.header} = {v} 超出合理上限 {col.upper:g}{hint}", col.header)
            )
            continue
        payload["nutrients"][col.key] = {
            "v": round(v, 4),
            "unit": col.unit,
            "nid": col.nutrient_id,
            "label": NUTRIENT_LABELS.get(col.key, col.key),
        }

    # ---------- 交叉校验 ----------
    nut = payload["nutrients"]
    p = (nut.get("protein_g") or {}).get("v")
    f = (nut.get("fat_g") or {}).get("v")
    c = (nut.get("carb_g") or {}).get("v")
    e = (nut.get("energy_kcal") or {}).get("v")
    if None not in (p, f, c):
        total = p + f + c
        if total > 105:
            msgs.append(
                Message("warn", f"蛋白质+脂肪+碳水 = {total:.1f}g/100g，超过 100g，请核对是否按每 100g 填写", "碳水化合物(g)")
            )
        if e is not None:
            # 4/9/4 是 Atwater 系数，膳食纤维按 2 计入可让误差更小，这里只用三大宏量做粗校
            calc = 4 * p + 9 * f + 4 * c
            if calc >= 20 and abs(e - calc) / calc > 0.5:
                msgs.append(
                    Message(
                        "warn",
                        f"能量 {e:g} kcal 与三大宏量推算的 {calc:.0f} kcal 差得多，可能填错行或单位不对",
                        "能量(kcal)",
                    )
                )

    if sum(1 for v in nut.values()) < 4:
        msgs.append(Message("error", f"营养素只有 {len(nut)} 项，至少需要 4 项", "营养素"))

    has_error = any(m.level == "error" for m in msgs)
    if has_error:
        verdict = "error"
    elif any(m.level == "warn" for m in msgs):
        verdict = "warn"
    else:
        verdict = "ok"

    return RowResult(row_no=row_no, name=name, verdict=verdict, messages=msgs, payload=payload)


def validate_table(table: ParsedTable, *, relax_required: bool = False) -> list[RowResult]:
    """整表校验。只做文件内去重，与数据库的比对交给调用方（需要查库）。"""
    results: list[RowResult] = []
    seen: dict[str, int] = {}
    for row_no, row in table.rows:
        r = validate_row(row_no, row, table.mapped, relax_required=relax_required)
        if r.verdict != "error" and r.payload["name_norm"]:
            first = seen.get(r.payload["name_norm"])
            if first is not None:
                r.verdict = "duplicate_file"
                r.messages.append(
                    Message("warn", f"与第 {first} 行同名，本行跳过（只导入第一行）", "名称")
                )
            else:
                seen[r.payload["name_norm"]] = row_no
        results.append(r)
    return results


def summarize(results: list[RowResult]) -> dict[str, int]:
    return {
        "total": len(results),
        "ok": sum(1 for r in results if r.verdict == "ok"),
        "warn": sum(1 for r in results if r.verdict == "warn"),
        "error": sum(1 for r in results if r.verdict == "error"),
        "duplicate_file": sum(1 for r in results if r.verdict == "duplicate_file"),
        "duplicate_db": sum(1 for r in results if r.verdict == "duplicate_db"),
        "importable": sum(1 for r in results if r.importable),
    }


def sample_rows() -> list[list[Any]]:
    """模板里的示例行。

    三条刻意选成不同形态：一条完整、一条缺可选营养素（示范空值不是错）、
    一条性味留空（说明可选字段）。

    按 **列 key** 写，不按位置写：营养素列已经加到 28 个，以后再插入一列，
    位置式写法会把示例值整体错位一位 —— 而错位不会报错，只会让模板里的
    示例变成一眼看不出错的脏数据。
    """
    rows: list[dict[str, Any]] = [
        {
            "name": "藜麦", "aliases": "南美藜;昆诺阿藜", "category": "谷物",
            "nature": "平", "english_name": "quinoa",
            "scientific_name": "Chenopodium quinoa",
            "energy_kcal": 368, "protein_g": 14.1, "fat_g": 6.1, "carb_g": 64.2,
            "fiber_g": 7.0, "sodium_mg": 5, "calcium_mg": 47, "iron_mg": 4.6,
            "zinc_mg": 3.1, "potassium_mg": 563, "magnesium_mg": 197,
            "vitamin_c_mg": 0, "vitamin_a_ug": 0, "carotene_ug": 0, "purine_mg": 0,
            "source": "中国食物成分表 第6版 第1册",
            "note": "示例：一条完整记录",
        },
        {
            "name": "北豆腐", "aliases": "老豆腐;硬豆腐", "category": "豆类",
            "nature": "凉", "english_name": "firm tofu",
            "energy_kcal": 116, "protein_g": 12.2, "fat_g": 4.8, "carb_g": 3.0,
            "fiber_g": 0.5, "sodium_mg": 7, "calcium_mg": 138, "iron_mg": 1.9,
            "zinc_mg": 0.7, "potassium_mg": 106, "magnesium_mg": 63,
            "vitamin_c_mg": 0, "vitamin_a_ug": 0, "carotene_ug": 0, "purine_mg": 0,
            "source": "中国食物成分表 第6版 第1册",
            "note": "示例：可选营养素留空即可",
        },
        {
            "name": "西芹", "aliases": "芹菜;旱芹", "category": "蔬菜",
            "nature": "", "english_name": "celery",
            "scientific_name": "Apium graveolens",
            "energy_kcal": 17, "protein_g": 0.8, "fat_g": 0.1, "carb_g": 3.9,
            "fiber_g": 1.2, "sodium_mg": 159, "calcium_mg": 48, "iron_mg": 0.8,
            "zinc_mg": 0.2, "potassium_mg": 206, "magnesium_mg": 18,
            "vitamin_c_mg": 8, "vitamin_a_ug": 22, "carotene_ug": 260, "purine_mg": 0,
            "source": "中国食物成分表 第6版 第1册",
            "note": "示例：性味留空也可以",
        },
    ]
    return [[r.get(c.key, "") for c in COLUMNS] for r in rows]
