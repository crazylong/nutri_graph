"""
kg/query.py —— 按意图选择跳数的 Cypher 查询模板

设计要点：
  - 每个模板都带 evidence_type 过滤（WHERE r.evidence_type IN $allowed）
  - 用户过敏/慢病硬过滤（NOT b.canonical_name IN $allergens）
  - 查询结果连同 evidence_level 一起返回，供生成阶段引用
  - **provisional 过滤**：$include_provisional 为 false 时排除单源未交叉验证的边。
    默认 true（刚补录完要查得到，否则闭环断裂），但数据本身会带 provisional 标记，
    由生成阶段降档加提示 —— 隐藏和降级是两回事，见 kg/promote.py 的说明。
"""
from __future__ import annotations

from typing import Any

from neo4j import GraphDatabase

import config

# 三重引号里字符串拼接太碎，provisional 条件抽成常量，所有模板共用同一份，
# 避免「改了一个模板忘了另一个」这种最难查的漏。
_PROV = "AND ($include_provisional OR coalesce(r.provisional, false) = false)"

# 意图 → Cypher 模板
# $cid: 食材 canonical_id；$allowed: 允许的 evidence_type 列表；$k: top-k；
# $allergens: 排除列表；$include_provisional: 是否包含单源数据

TEMPLATES: dict[str, str] = {
    "pair": f"""
        MATCH (a:Ingredient {{canonical_id: $cid}})-[r:RECOMMEND_PAIR]-(b:Ingredient)
        WHERE r.evidence_type IN $allowed AND NOT b.canonical_name IN $allergens
              {_PROV}
        RETURN b.canonical_id AS cid, b.canonical_name AS name,
               r.evidence_type AS etype, r.evidence_level AS elevel,
               r.claim_text AS claim, r.effect AS effect,
               coalesce(r.provisional, false) AS provisional
        ORDER BY r.evidence_level, r.confidence DESC LIMIT $k
    """,
    "contra_population": f"""
        MATCH (a:Ingredient {{canonical_id: $cid}})-[r:CONTRA_POPULATION]->(p:Population)
        WHERE r.evidence_type IN $allowed {_PROV}
        OPTIONAL MATCH (p)-[:HAS_SYMPTOM]->(s:Symptom)
        RETURN p.canonical_id AS cid, p.name AS name, collect(DISTINCT s.name) AS symptoms,
               r.evidence_type AS etype, r.evidence_level AS elevel, r.claim_text AS claim,
               coalesce(r.provisional, false) AS provisional
        ORDER BY r.evidence_level LIMIT $k
    """,
    "contra_item": f"""
        MATCH (a:Ingredient {{canonical_id: $cid}})-[r:CONTRA_ITEM]->(t:Taboo)
        WHERE r.evidence_type IN $allowed {_PROV}
        RETURN t.canonical_id AS cid, t.name AS name, t.taboo_type AS ttype,
               r.evidence_type AS etype, r.evidence_level AS elevel, r.claim_text AS claim,
               coalesce(r.provisional, false) AS provisional
        ORDER BY r.evidence_level LIMIT $k
    """,
    "nutrition": f"""
        MATCH (a:Ingredient {{canonical_id: $cid}})-[r:CONTAINS_NUTRIENT]->(n:Nutrient)
        WHERE r.evidence_type IN $allowed {_PROV}
        RETURN n.canonical_id AS cid, n.name AS name, n.unit AS unit,
               r.amount AS amount, r.claim_text AS claim,
               r.evidence_type AS etype, r.evidence_level AS elevel,
               coalesce(r.provisional, false) AS provisional
        ORDER BY r.evidence_level LIMIT $k
    """,
    "substitute": f"""
        MATCH (a:Ingredient {{canonical_id: $cid}})-[r:SUBSTITUTE_FOR]-(b:Ingredient)
        WHERE r.evidence_type IN $allowed AND NOT b.canonical_name IN $allergens
              {_PROV}
        RETURN b.canonical_id AS cid, b.canonical_name AS name,
               r.evidence_type AS etype, r.evidence_level AS elevel,
               r.claim_text AS claim, r.basis AS basis,
               coalesce(r.provisional, false) AS provisional
        ORDER BY r.evidence_level LIMIT $k
    """,
    # 反向查：什么食材对某症状有帮助（先通过症状找人群，再找食材，用于「上火了吃什么」）
    "symptom_remedy": f"""
        MATCH (s:Symptom {{canonical_id: $cid}})
        OPTIONAL MATCH (s)<-[:HAS_SYMPTOM]-(p:Population)
        OPTIONAL MATCH (p)<-[r:CONTRA_POPULATION]-(avoid:Ingredient)
        WHERE $include_provisional OR coalesce(r.provisional, false) = false
        WITH s, p, collect(DISTINCT avoid.canonical_name) AS avoid_names
        RETURN s.name AS symptom, p.name AS population, avoid_names
    """,
}

# 意图路由关键词（与 msb_assistant 的 KEYWORD_TABLE 风格一致）
INTENT_KEYWORDS: dict[str, list[str]] = {
    "pair": ["搭配", "配", "一起吃", "一起吃好不好", "相配", "适合一起", "配什么"],
    "contra_population": ["忌口", "不能吃", "慎食", "少吃", "禁忌", "痛风", "糖尿", "孕妇", "高血压", "肾"],
    "contra_item": ["药物", "药", "相互作用", "同服", "影响药效", "他汀", "华法林"],
    "nutrition": ["营养", "含什么", "成分", "维生素", "钙", "铁", "蛋白质", "营养素", "多少"],
    "substitute": ["替代", "代替", "换什么", "不吃这个吃什么", "平替"],
    "symptom_remedy": ["上火", "犯困", "失眠", "便秘", "咳嗽", "吃什么好", "调理", "症状", "食补", "补"],
}


class GraphQuery:
    def __init__(self):
        self._driver = None

    @property
    def driver(self):
        if self._driver is None:
            from neo4j import GraphDatabase

            self._driver = GraphDatabase.driver(
                config.NEO4J_URI, auth=(config.NEO4J_USER, config.NEO4J_PASSWORD)
            )
        return self._driver

    def run(
        self,
        intent: str,
        cid: str,
        allowed: list[str],
        allergens: list[str] | None = None,
        include_provisional: bool | None = None,
    ) -> list[dict[str, Any]]:
        if intent not in TEMPLATES:
            return []
        allergens = allergens or []
        if include_provisional is None:
            include_provisional = config.INCLUDE_PROVISIONAL
        with self.driver.session() as s:
            result = s.run(
                TEMPLATES[intent],
                cid=cid,
                allowed=allowed,
                k=config.GRAPH_MAX_RESULTS,
                allergens=allergens,
                include_provisional=include_provisional,
            )
            return [dict(r) for r in result]


def detect_intent(question: str) -> list[str]:
    """关键词意图检测，返回可能意图列表（按优先级）。"""
    hits = []
    for intent, kws in INTENT_KEYWORDS.items():
        if any(kw in question for kw in kws):
            hits.append(intent)
    return hits or ["pair"]  # 默认走搭配
