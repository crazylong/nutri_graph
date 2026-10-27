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

编号约定：NUT_00001~00020 是人工种子（data/crosswalk/nutrient_map.csv），
补录新增的从 NUT_00021 起固定分配，见 EXTRA_NUTRIENTS。
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
    """返回该营养素对应的图谱节点；None 表示不收录（未知或语义重复）。"""
    key = key_of(field_name, unit)
    if not key or key in SKIP_KEYS:
        return None
    return _EXISTING.get(key) or EXTRA_NUTRIENTS.get(key)
