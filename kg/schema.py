"""
kg/schema.py —— 图谱 Schema 定义

节点（5 类）：Ingredient / Nutrient / Population / Symptom / Taboo
边（5 主 + 2 辅助）：
    CONTAINS_NUTRIENT / RECOMMEND_PAIR / INCOMPATIBLE_WITH /
    CONTRA_POPULATION / SUBSTITUTE_FOR / CONTRA_ITEM / HAS_SYMPTOM

核心设计：证据「四件套」统一挂在所有关系上，是两层正交维度：
    evidence_type  : guideline | measured | traditional | anecdotal  （哪一类证据）
    evidence_level : A | B | C | D                                    （多可信）
    sources        : [{type,title,publisher,year,url,locator}]        （出处）
    claim_text     : 原始断言文本（用于引用与回溯）
"""
from __future__ import annotations

from enum import Enum
from typing import Any


# ============================================================
# 节点标签
# ============================================================
class NodeLabel(str, Enum):
    INGREDIENT = "Ingredient"   # 食材
    NUTRIENT = "Nutrient"       # 营养素
    POPULATION = "Population"   # 人群（谁：孕妇/痛风患者）
    SYMPTOM = "Symptom"         # 症状（含口语/TCM 术语）
    TABOO = "Taboo"             # 禁忌对象（那件事：他汀类药物/高尿酸血症）


# ============================================================
# 关系类型
# ============================================================
class RelType(str, Enum):
    CONTAINS_NUTRIENT = "CONTAINS_NUTRIENT"   # 含有营养素  Ingredient→Nutrient
    RECOMMEND_PAIR = "RECOMMEND_PAIR"         # 推荐搭配    Ingredient↔Ingredient
    INCOMPATIBLE_WITH = "INCOMPATIBLE_WITH"   # 不宜同食    Ingredient↔Ingredient
    CONTRA_POPULATION = "CONTRA_POPULATION"   # 人群禁忌    Ingredient→Population
    SUBSTITUTE_FOR = "SUBSTITUTE_FOR"         # 替代食材    Ingredient→Ingredient
    CONTRA_ITEM = "CONTRA_ITEM"               # 禁忌事项    Ingredient→Taboo（辅助）
    HAS_SYMPTOM = "HAS_SYMPTOM"               # 关联症状    Population→Symptom（辅助）


# ============================================================
# 证据分层
# ============================================================
class EvidenceType(str, Enum):
    GUIDELINE = "guideline"     # 指南/临床共识/权威机构
    MEASURED = "measured"       # 实验测定/人群研究
    TRADITIONAL = "traditional" # 传统典籍/药食同源目录
    ANECDOTAL = "anecdotal"     # 民间流传/网络说法（未验证）


class EvidenceLevel(str, Enum):
    A = "A"  # 指南/临床共识/权威机构警示
    B = "B"  # 实验测定/人群研究
    C = "C"  # 传统典籍/药食同源目录
    D = "D"  # 民间流传/网络说法（未验证）


# 各意图默认允许的 evidence_type（与 config.EVIDENCE_POLICY_MAP 对齐）
DEFAULT_POLICY: dict[str, list[str]] = {
    "pair": [EvidenceType.GUIDELINE, EvidenceType.MEASURED, EvidenceType.TRADITIONAL],
    "contra_population": [EvidenceType.GUIDELINE, EvidenceType.MEASURED],
    "contra_item": [EvidenceType.GUIDELINE, EvidenceType.MEASURED],
    "nutrition": [EvidenceType.GUIDELINE, EvidenceType.MEASURED],
    "substitute": [EvidenceType.GUIDELINE, EvidenceType.MEASURED, EvidenceType.TRADITIONAL],
}


# ============================================================
# 节点属性模板
# ============================================================
NODE_PROPS: dict[str, list[str]] = {
    "Ingredient": [
        "canonical_id", "canonical_name", "aliases", "category",
        "nature",          # 性味（寒/凉/平/温/热）
        "summary",         # ≤40 字摘要，进上下文
        "detail",          # 长文，仅详情 API
        "evidence_profile",  # {"total":n,"by_type":{...},"strongest_level":"A","contested":n}
    ],
    "Nutrient": ["canonical_id", "name", "unit", "category"],
    "Population": ["canonical_id", "name", "kind"],  # kind: group | disease
    "Symptom": ["canonical_id", "name", "aliases", "tcm_term"],
    "Taboo": ["canonical_id", "name", "taboo_type"],  # drug | disease_state | cooking | extreme_pair
}

# 关系公共属性（证据四件套 + 审计）
REL_COMMON_PROPS: list[str] = [
    "evidence_type", "evidence_level", "sources", "claim_text",
    "confidence", "batch_id", "review_status", "created_by", "relation_key",
]

# 各关系的额外属性
REL_EXTRA_PROPS: dict[str, list[str]] = {
    "CONTAINS_NUTRIENT": ["amount", "unit", "per"],
    "RECOMMEND_PAIR": ["effect", "rationale"],
    "INCOMPATIBLE_WITH": ["reason", "severity"],
    "CONTRA_POPULATION": ["condition"],
    "SUBSTITUTE_FOR": ["basis", "similarity"],
    "CONTRA_ITEM": ["interaction"],
    "HAS_SYMPTOM": ["relation"],
}


def build_evidence(
    evidence_type: str,
    evidence_level: str,
    sources: list[dict[str, Any]] | None = None,
    claim_text: str = "",
) -> dict[str, Any]:
    """构造统一的证据四件套。"""
    return {
        "evidence_type": evidence_type,
        "evidence_level": evidence_level,
        "sources": sources or [],
        "claim_text": claim_text,
    }


def sources_to_json(sources: list[dict[str, Any]] | None) -> str:
    """把 sources 序列化成 JSON 字符串 —— **写 Neo4j 前必须过这一步**。

    Neo4j 的属性只支持基本类型和基本类型数组，**不支持 map**。
    直接 `SET r.sources = [{...}]` 会抛：
      CypherTypeError: Property values can only be of primitive types or arrays thereof

    所以图里的 sources 一律是 JSON 字符串，读出来再 json.loads。
    PostgreSQL 侧（kg_candidate.sources）保持 JSONB，不受此限 ——
    这一层差异必须记住，否则「本地测试通了、真跑图就炸」。
    """
    import json

    return json.dumps(sources or [], ensure_ascii=False)


def relation_key(src_cid: str, rel: str, dst_cid: str) -> str:
    """关系业务键 —— 幂等写入的锚点（Neo4j 无关系唯一约束，用此键做确定性 MERGE）。"""
    import hashlib

    raw = f"{src_cid}|{rel}|{dst_cid}".encode("utf-8")
    return hashlib.sha1(raw).hexdigest()  # noqa: S324 —— 非安全场景，仅做去重键


# canonical_id 前缀
CID_PREFIX = {
    "Ingredient": "ING",
    "Nutrient": "NUT",
    "Population": "POP",
    "Symptom": "SYM",
    "Taboo": "TAB",
}


def make_canonical_id(label: str, num: int) -> str:
    """生成 canonical_id，例如 make_canonical_id("Ingredient", 1) -> ING_00001"""
    return f"{CID_PREFIX[label]}_{num:05d}"
