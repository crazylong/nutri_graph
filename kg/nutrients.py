"""
kg/nutrients.py —— 外部营养素 → 图谱 Nutrient 节点的映射

为什么需要这一层：USDA 吐的是 nutrient.id（1003/1008/…）+ 英文名，
而图谱里是 NUT_000xx + 中文名。两者必须有个**确定的**对应关系，
否则同一个「蛋白质」会因为一条来自种子、一条来自补录而变成两个节点 ——
和「西红柿 / 番茄」不能是两个节点是同一类错误。

原则：
  1. 能对上既有节点的，一律复用（蛋白质 / 脂肪 / 碳水 / 膳食纤维）
  2. 对不上的，按固定 canonical_id 新建，**不动态分配**
     —— 动态分配不幂等，两次审核会造出两个「能量」节点
  3. 同一含义只留一个：「能量」同时有 kcal 与 kJ，图里只收 kcal
     （两条 unit 不同的「能量」边进上下文只会让模型困惑）

编号约定：
  * NUT_00001~00020 是第一批人工种子（data/crosswalk/nutrient_map.csv）；
  * NUT_00021 / NUT_00022 是补录链路新增的能量（kcal）与钠，见 EXTRA_NUTRIENTS；
  * NUT_00023~00034 是第二批种子，覆盖中国食物成分表里图谱原本没有的那些
    （磷/硒/铜/锰/碘/胆固醇/B2/烟酸/维 E/三种脂肪酸），同一份 CSV 里登记。
  **不要跳号或复用**：这个 id 是「同一营养素在任何来源下都是同一个节点」的唯一保证。
"""
from __future__ import annotations

from dataclasses import dataclass

from kg.usda import NUTRIENTS


@dataclass(frozen=True)
class NutrientRef:
    canonical_id: str
    name: str
    unit: str
    category: str
    is_new: bool          # True = 图里还没有，审核通过时需要一并建节点


# USDA nutrient.id → (内部 key, 中文名, 单位)，所以中文名是 [1]、单位是 [2]
_BY_NAME_UNIT: dict[tuple[str, str], str] = {
    (name, unit): key for key, name, unit in NUTRIENTS.values()
}

# 已有节点：直接复用
_EXISTING: dict[str, NutrientRef] = {
    "protein": NutrientRef("NUT_00012", "蛋白质", "g", "宏量营养素", False),
    "fat": NutrientRef("NUT_00013", "脂肪", "g", "宏量营养素", False),
    "carb": NutrientRef("NUT_00014", "碳水化合物", "g", "宏量营养素", False),
    "fiber": NutrientRef("NUT_00011", "膳食纤维", "g", "其他", False),
}

# 人工种子里的 20 个营养素（data/crosswalk/nutrient_map.csv），图里已经有这些节点。
#
# 为什么必须补这张表：USDA 只吐 7 项（能量/蛋白/脂肪/碳水/纤维/钠），
# 而 Excel 导入能填 15 项 —— 钙、铁、锌、维生素C 都在 USDA 那份词表之外，
# 原来 resolve() 对它们一律返回 None，审核时被静默跳过，导入页填了也白填。
# 键是 **(中文名, 单位)**，和 kg/usda.py 的 NUTRIENTS 用同一套命名，
# 所以 bundle item 里存什么，这里就能对上什么。
_SEEDED: dict[tuple[str, str], NutrientRef] = {
    ("维生素C", "mg"): NutrientRef("NUT_00001", "维生素C", "mg", "维生素", False),
    ("维生素A", "μgRAE"): NutrientRef("NUT_00002", "维生素A", "μgRAE", "维生素", False),
    ("维生素B1", "mg"): NutrientRef("NUT_00003", "维生素B1", "mg", "维生素", False),
    ("叶酸", "μg"): NutrientRef("NUT_00004", "叶酸", "μg", "维生素", False),
    ("维生素K", "μg"): NutrientRef("NUT_00005", "维生素K", "μg", "维生素", False),
    ("钙", "mg"): NutrientRef("NUT_00006", "钙", "mg", "矿物质", False),
    ("铁", "mg"): NutrientRef("NUT_00007", "铁", "mg", "矿物质", False),
    ("锌", "mg"): NutrientRef("NUT_00008", "锌", "mg", "矿物质", False),
    ("钾", "mg"): NutrientRef("NUT_00009", "钾", "mg", "矿物质", False),
    ("镁", "mg"): NutrientRef("NUT_00010", "镁", "mg", "矿物质", False),
    ("膳食纤维", "g"): NutrientRef("NUT_00011", "膳食纤维", "g", "其他", False),
    ("蛋白质", "g"): NutrientRef("NUT_00012", "蛋白质", "g", "宏量营养素", False),
    ("脂肪", "g"): NutrientRef("NUT_00013", "脂肪", "g", "宏量营养素", False),
    ("碳水化合物", "g"): NutrientRef("NUT_00014", "碳水化合物", "g", "宏量营养素", False),
    ("胡萝卜素", "μg"): NutrientRef("NUT_00015", "胡萝卜素", "μg", "植物化学物", False),
    ("番茄红素", "mg"): NutrientRef("NUT_00016", "番茄红素", "mg", "植物化学物", False),
    ("花青素", "mg"): NutrientRef("NUT_00017", "花青素", "mg", "植物化学物", False),
    ("ω-3脂肪酸", "g"): NutrientRef("NUT_00018", "ω-3脂肪酸", "g", "脂肪酸", False),
    ("嘌呤", "mg"): NutrientRef("NUT_00019", "嘌呤", "mg", "其他", False),
    ("咖啡因", "mg"): NutrientRef("NUT_00020", "咖啡因", "mg", "其他", False),
    # ---------- 第二批种子（2026-10-28）----------
    # 为什么补这一批：中国食物成分表这份数据源里有 31 项营养素，但图谱只认 13 项，
    # 其余（磷、硒、铜、锰、碘、胆固醇、B2、烟酸、维 E、三种脂肪酸）在 resolve()
    # 里返回 None，导入时被静默跳过 —— 数据抓到了，图上没有，等于没导。
    # 单位一律照原表：%E = 占总能量百分比，**不是克**，不能和「脂肪 g」混着比。
    ("磷", "mg"): NutrientRef("NUT_00023", "磷", "mg", "矿物质", False),
    ("维生素B2", "mg"): NutrientRef("NUT_00024", "维生素B2", "mg", "维生素", False),
    ("烟酸", "mg"): NutrientRef("NUT_00025", "烟酸", "mg", "维生素", False),
    ("硒", "μg"): NutrientRef("NUT_00026", "硒", "μg", "矿物质", False),
    ("铜", "mg"): NutrientRef("NUT_00027", "铜", "mg", "矿物质", False),
    ("胆固醇", "mg"): NutrientRef("NUT_00028", "胆固醇", "mg", "其他", False),
    ("锰", "mg"): NutrientRef("NUT_00029", "锰", "mg", "矿物质", False),
    ("维生素E", "mg"): NutrientRef("NUT_00030", "维生素E", "mg", "维生素", False),
    ("碘", "μg"): NutrientRef("NUT_00031", "碘", "μg", "矿物质", False),
    ("饱和脂肪酸", "%E"): NutrientRef("NUT_00032", "饱和脂肪酸", "%E", "脂肪酸", False),
    ("单不饱和脂肪酸", "%E"): NutrientRef(
        "NUT_00033", "单不饱和脂肪酸", "%E", "脂肪酸", False
    ),
    ("多不饱和脂肪酸", "%E"): NutrientRef(
        "NUT_00034", "多不饱和脂肪酸", "%E", "脂肪酸", False
    ),
}

# 新节点：固定 id（见文件头第 2、3 条）
EXTRA_NUTRIENTS: dict[str, NutrientRef] = {
    "energy_kcal": NutrientRef("NUT_00021", "能量", "kcal", "宏量营养素", True),
    "sodium": NutrientRef("NUT_00022", "钠", "mg", "矿物质", True),
}

# 语义重复、不单独建边的 key
SKIP_KEYS: frozenset[str] = frozenset({"energy_kj"})


def key_of(field_name: str, unit: str) -> str:
    """从 bundle item 的 (中文名, 单位) 反查 USDA key。查不到返回空串。

    bundle item 只存了展示用的中文名和单位，没有存 key。
    不额外加一列的理由：这两个值都源自 kg.usda.NUTRIENTS，反查是精确的，
    加一列反而要改 9.2 的落库代码和一整批历史数据。
    """
    return _BY_NAME_UNIT.get((field_name, unit), "")


def resolve(field_name: str, unit: str) -> NutrientRef | None:
    """返回该营养素对应的图谱节点；None 表示不收录（未知或语义重复）。

    两条来源都能对上：
      * USDA 抓取 —— 走 (中文名, 单位) → USDA key → 固定 id
      * Excel 导入 —— 走 _SEEDED（人工种子 20 项）
    查不到就是真的没收录，调用方会记日志跳过。
    """
    name = (field_name or "").strip()
    unit = (unit or "").strip()

    key = key_of(name, unit)
    if key:
        if key in SKIP_KEYS:
            return None
        ref = _EXISTING.get(key) or EXTRA_NUTRIENTS.get(key)
        if ref is not None:
            return ref

    return _SEEDED.get((name, unit))
