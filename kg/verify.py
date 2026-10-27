"""
kg/verify.py —— 「B 步骤」：把抓到的产物打分并决定去留

和原方案最大的不同：**不调用模型给自己打分**。

原设计是「B 模型给一个 0.9 分」，但 LLM 自评不是概率、未校准，
同一家族的两个模型还会互相确认幻觉。所以这里改成可解释的规则：

  score = 完整性 0.20 × 权威性 0.30 × 一致性 0.30 × 无冲突 0.20

其中「一致性」在当前只接一个源（USDA）时恒为 0.6，
算下来 0.20 + 0.30 + 0.18 + 0.20 = **0.88**，卡在 0.9 门槛下面 ——
这正是想要的效果：**只有单源就绝不自动通过**，等接入第二个来源再谈自动。

关于 grounding：结构化 API 没有「散文正文」可做蕴含判断，
所以这里的可追溯性退化为「每个数值都能指回原始响应」（source_locator = fdc:174270#1003）。
真正需要 span 匹配的是网页抓取，那是离线导入的事。
这一点必须写清楚，不能假装做了语义 grounding。
"""
from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Any

import config

log = logging.getLogger("kg.verify")

# 权威性：白名单在代码里，不交给模型判断
SOURCE_AUTHORITY: dict[str, float] = {
    "usda_fdc": 1.0,     # 美国农业部，公有领域
    "china_fcd": 1.0,    # 中国疾控中心食物成分表（离线导入）
    "web": 0.3,          # 通用网页，只作线索
}

REQUIRED_FIELDS = ("canonical_name", "english_name", "category")
MIN_NUTRIENTS = 4


@dataclass
class VerifyResult:
    score_total: float
    score_detail: dict[str, float]
    hard_veto: str
    status: str                       # approved | pending_review
    reasons: list[str] = field(default_factory=list)

    @property
    def auto_approved(self) -> bool:
        return self.status == "approved"


def verify(
    draft: dict[str, Any],
    nutrients: list[dict],
    source_key: str,
    source_count: int = 1,
    match_veto: str = "",
    conflict_alias: str = "",
) -> VerifyResult:
    """draft 用 as_dict() 的结果，nutrients 用 kg.usda.parse_nutrients 的结果。"""
    reasons: list[str] = []
    vetoes: list[str] = []

    # ---------- 1. 完整性 ----------
    present = sum(1 for f in REQUIRED_FIELDS if str(draft.get(f) or "").strip())
    fields_ratio = present / len(REQUIRED_FIELDS)
    nutrient_ratio = min(1.0, len(nutrients) / MIN_NUTRIENTS)
    completeness = round(fields_ratio * 0.6 + nutrient_ratio * 0.4, 4)
    if fields_ratio < 1.0:
        missing = [f for f in REQUIRED_FIELDS if not str(draft.get(f) or "").strip()]
        vetoes.append(f"必需字段缺失：{'/'.join(missing)}")
    if len(nutrients) < MIN_NUTRIENTS:
        vetoes.append(f"营养素不足 {MIN_NUTRIENTS} 项（实得 {len(nutrients)}）")

    # ---------- 2. 权威性 ----------
    authority = SOURCE_AUTHORITY.get(source_key, 0.0)
    if authority == 0.0:
        vetoes.append(f"来源未在白名单内：{source_key}")

    # ---------- 3. 一致性 ----------
    # 单源封顶 0.6：不是"不确定"，而是"无法交叉验证"
    agreement = 1.0 if source_count >= 2 else 0.6
    if source_count < 2:
        reasons.append("单一来源，未做交叉验证")

    # ---------- 4. 无冲突 ----------
    conflict = 0.0 if conflict_alias else 1.0
    if conflict_alias:
        vetoes.append(f"与图谱既有条目冲突：{conflict_alias}")

    # ---------- 5. 匹配阶段的硬否决 ----------
    if match_veto:
        vetoes.append(match_veto)

    score = round(
        completeness * 0.20 + authority * 0.30 + agreement * 0.30 + conflict * 0.20,
        4,
    )

    hard_veto = "；".join(vetoes)
    auto_ok = (
        not hard_veto
        and score >= config.FOOD_AUTO_APPROVE_MIN_SCORE
        and source_count >= config.FOOD_AUTO_APPROVE_MIN_SOURCES
    )
    if not hard_veto and not auto_ok:
        reasons.append(
            f"得分 {score} < {config.FOOD_AUTO_APPROVE_MIN_SCORE}"
            f" 或来源数 {source_count} < {config.FOOD_AUTO_APPROVE_MIN_SOURCES}，转人工"
        )

    return VerifyResult(
        score_total=score,
        score_detail={
            "completeness": completeness,
            "authority": authority,
            "agreement": agreement,
            "no_conflict": conflict,
        },
        hard_veto=hard_veto,
        status="approved" if auto_ok else "pending_review",
        reasons=reasons,
    )
