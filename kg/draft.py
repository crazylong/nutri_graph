"""
kg/draft.py —— 「A 模型」：把中文食材名变成可在权威库里检索的画像

这一步是整个补录链路里唯一需要模型判断的地方，所以要约束得很死：
输出必须是固定字段的 JSON，而且**必须给出「身份关键词」和「排除词」**。

为什么要 `identity_keywords` / `exclude_keywords` 两个字段：

  「黑豆」的正确身份关键词是 soybean（Glycine max，大豆属），
  而 USDA 里的 "Beans, black, mature seeds, raw" 是黑菜豆（Phaseolus vulgaris，菜豆属）。
  两者营养差异明显，但**同属 "Legumes and Legume Products" 分类** ——
  靠分类根本区分不开（实测确认）。唯一的判别信号是描述首词：
  正确的候选项一定含 soybean，错误的候选项一定含 black。

  所以校准锚点必须由「这个东西独有的词」承担，不能指望模型的英文名翻译
  —— 一旦模型把「黑豆」翻成 "black beans"，检索就会一击命中错误条目，
  这时只有 exclude_keywords 能把它拦下来。

性味（寒热温凉）刻意不在这里抽：USDA 和食物成分表都没有这个维度，
让模型凭记忆给一个「性平」等于编造，必须来自药食同源目录 / 典籍（离线导入）。
"""
from __future__ import annotations

import json
import logging
import re
from dataclasses import dataclass, field

import config

log = logging.getLogger("kg.draft")


class DraftError(RuntimeError):
    """拿不到可用的检索画像（模型不可用 / 输出不合规）。"""


@dataclass
class FoodDraft:
    canonical_name: str                      # 中文规范名
    english_name: str                        # 英文名（人读）
    fdc_query: str                           # 给 USDA 的检索式（靠描述匹配）
    identity_keywords: list[str] = field(default_factory=list)   # 必须有其一
    exclude_keywords: list[str] = field(default_factory=list)    # 命中即否决
    scientific_name: str = ""                # 学名（用于核对与展示）
    category: str = ""                       # 中文分类
    aliases: list[str] = field(default_factory=list)
    source: str = "llm"                      # llm | static

    def as_dict(self) -> dict:
        return {
            "canonical_name": self.canonical_name,
            "english_name": self.english_name,
            "fdc_query": self.fdc_query,
            "identity_keywords": self.identity_keywords,
            "exclude_keywords": self.exclude_keywords,
            "scientific_name": self.scientific_name,
            "category": self.category,
            "aliases": self.aliases,
            "source": self.source,
        }


# ============================================================
# 内置画像（离线可用，也是测试基线）
# ============================================================
_STATIC: dict[str, FoodDraft] = {
    "黑豆": FoodDraft(
        canonical_name="黑豆",
        english_name="soybeans",
        fdc_query="soybeans mature seeds raw",
        identity_keywords=["soybean"],
        exclude_keywords=["black", "turtle", "mung", "adzuki", "kidney", "pinto", "cannellini"],
        scientific_name="Glycine max",
        category="豆类",
        aliases=["黑大豆", "乌豆", "黑小豆"],
        source="static",
    ),
    "黄豆": FoodDraft(
        canonical_name="黄豆",
        english_name="soybeans",
        fdc_query="soybeans mature seeds raw",
        identity_keywords=["soybean"],
        exclude_keywords=["black", "turtle", "mung", "adzuki", "kidney", "pinto"],
        scientific_name="Glycine max",
        category="豆类",
        aliases=["大豆"],
        source="static",
    ),
    "鹰嘴豆": FoodDraft(
        canonical_name="鹰嘴豆",
        english_name="chickpeas",
        fdc_query="chickpeas mature seeds raw",
        identity_keywords=["chickpea", "garbanzo"],
        exclude_keywords=["black", "soybean"],
        scientific_name="Cicer arietinum",
        category="豆类",
        source="static",
    ),
    "藜麦": FoodDraft(
        canonical_name="藜麦",
        english_name="quinoa",
        fdc_query="quinoa uncooked",
        identity_keywords=["quinoa"],
        exclude_keywords=[],
        scientific_name="Chenopodium quinoa",
        category="谷物",
        source="static",
    ),
    "秋葵": FoodDraft(
        canonical_name="秋葵",
        english_name="okra",
        fdc_query="okra raw",
        identity_keywords=["okra"],
        exclude_keywords=["canned", "pickled"],
        scientific_name="Abelmoschus esculentus",
        category="蔬菜",
        source="static",
    ),
}

_PROMPT = """你是食物成分数据库的检索助手。请为下面这个中文食材生成一条检索画像。

食材：{food}

只输出 JSON，不要任何解释，不要 markdown 代码块。字段：
- canonical_name: 中文规范名
- english_name: 最通用的英文名
- fdc_query: 用于在美国农业部 FoodData Central（按英文描述索引）检索的一句话，
  形如 "<主料英文复数> mature seeds raw" 或 "<主料英文> raw"，尽量贴近数据库里的通用条目
- identity_keywords: 数组，**只有这个物种的描述里才会出现的词**（如 soybean / chickpea / quinoa）
- exclude_keywords: 数组，**中文名相近但其实是别的东西**的英文词，用来防止同名异物。
  例：中文「黑豆」是黑大豆（Glycine max），必须排除 black / turtle（那是黑菜豆 Phaseolus vulgaris）；
  中文「山药」要排除 yam 之外的 potato 类混淆
- scientific_name: 学名（拉丁名），不确定就留空字符串
- category: 中文分类（豆类 / 谷物 / 蔬菜 / 水果 / 坚果 / 菌藻 / 肉类 / 水产 / 蛋奶）
- aliases: 数组，中文别名 / 俗名

注意：exclude_keywords 一定要认真填。中文食材与英文数据库存在大量同名异物，
填错的后果是把另一个物种的营养数据挂到这个食材上。

JSON："""


def _parse_json(text: str) -> dict:
    text = (text or "").strip()
    # 模型偶尔会包一层 ```json
    text = re.sub(r"^```(?:json)?|```$", "", text, flags=re.MULTILINE).strip()
    try:
        return json.loads(text)
    except Exception as exc:  # noqa: BLE001
        start, end = text.find("{"), text.rfind("}")
        if start >= 0 and end > start:
            try:
                return json.loads(text[start : end + 1])
            except Exception:  # noqa: BLE001
                pass
        raise DraftError(f"模型输出不是合法 JSON：{text[:120]}") from exc


def _as_list(value) -> list[str]:
    if isinstance(value, str):
        return [v.strip() for v in re.split(r"[,，;；/]", value) if v.strip()]
    if isinstance(value, list):
        return [str(v).strip() for v in value if str(v).strip()]
    return []


def extract_llm(food: str) -> FoodDraft:
    """调模型生成画像。模型不可用或输出不合规都抛 DraftError，由上层把任务标失败。"""
    from llm import invoke_model

    resp = invoke_model(_PROMPT.format(food=food))
    data = _parse_json(getattr(resp, "content", resp))

    identity = _as_list(data.get("identity_keywords"))
    query = str(data.get("fdc_query") or "").strip()
    if not query or not identity:
        # 没有校准锚点的画像不能用 —— 等于放任同名异物通过
        raise DraftError(f"画像缺少 fdc_query 或 identity_keywords：{data}")

    return FoodDraft(
        canonical_name=str(data.get("canonical_name") or food).strip(),
        english_name=str(data.get("english_name") or "").strip(),
        fdc_query=query,
        identity_keywords=identity,
        exclude_keywords=_as_list(data.get("exclude_keywords")),
        scientific_name=str(data.get("scientific_name") or "").strip(),
        category=str(data.get("category") or "").strip(),
        aliases=_as_list(data.get("aliases")),
        source="llm",
    )


def get_draft(food: str, mode: str | None = None) -> FoodDraft:
    """按配置取画像。

    mode:
      static → 只用内置画像（离线、可测试、零成本）
      llm    → 只用模型
      auto   → 内置优先，没有再调模型（默认；练习/演示时最稳）
    """
    mode = (mode or config.FOOD_DRAFT_MODE).lower()
    if mode == "static" or (mode == "auto" and food in _STATIC):
        if food not in _STATIC:
            raise DraftError(f"内置画像里没有「{food}」，且当前是 static 模式")
        return _STATIC[food]
    if mode in ("llm", "auto"):
        return extract_llm(food)
    raise DraftError(f"未知的 FOOD_DRAFT_MODE：{mode}")


def known_static() -> list[str]:
    return sorted(_STATIC)
