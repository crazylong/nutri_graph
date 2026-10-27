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
    # 食材↔食材的民间不宜同食。和 RECOMMEND_PAIR 分开查，
    # 「能一起吃吗」才会同时看到推荐边和相克边。
    "incompatible": f"""
        MATCH (a:Ingredient {{canonical_id: $cid}})-[r:INCOMPATIBLE_WITH]-(b:Ingredient)
        WHERE r.evidence_type IN $allowed AND NOT b.canonical_name IN $allergens
              {_PROV}
        RETURN b.canonical_id AS cid, b.canonical_name AS name,
               r.evidence_type AS etype, r.evidence_level AS elevel,
               r.claim_text AS claim, r.reason AS effect,
               coalesce(r.provisional, false) AS provisional
        ORDER BY r.evidence_level, r.confidence DESC LIMIT $k
    """,
    # 症状 → 关联人群 → 这些人该避开的食材。
    # $cid 必须是 Symptom。图里没有「对症推荐吃什么」的边，
    # 能回答的是禁忌，不是食疗方。
    "symptom_remedy": f"""
        MATCH (s:Symptom {{canonical_id: $cid}})<-[:HAS_SYMPTOM]-(p:Population)
              <-[r:CONTRA_POPULATION]-(avoid:Ingredient)
        WHERE r.evidence_type IN $allowed {_PROV}
        RETURN avoid.canonical_id AS cid, avoid.canonical_name AS name,
               p.name AS population, s.name AS symptom,
               r.evidence_type AS etype, r.evidence_level AS elevel,
               r.claim_text AS claim,
               coalesce(r.provisional, false) AS provisional
        ORDER BY r.evidence_level LIMIT $k
    """,
}

# 意图路由关键词（与 msb_assistant 的 KEYWORD_TABLE 风格一致）
INTENT_KEYWORDS: dict[str, list[str]] = {
    "pair": ["搭配", "配", "一起吃", "一起吃好不好", "相配", "适合一起", "配什么"],
    # 「一起吃好」是在问搭配，不要算进不宜同食。只认明确的相克问法。
    "incompatible": ["不宜同食", "不能同吃", "相克", "能一起吃", "一起吃吗", "同食"],
    "contra_population": ["忌口", "不能吃", "慎食", "少吃", "禁忌", "痛风", "糖尿", "孕妇", "高血压", "肾"],
    "contra_item": ["药物", "药", "相互作用", "同服", "影响药效", "他汀", "华法林"],
    "nutrition": ["营养", "含什么", "成分", "维生素", "钙", "铁", "蛋白质", "营养素", "多少"],
    "substitute": ["替代", "代替", "换什么", "不吃这个吃什么", "平替"],
    "symptom_remedy": ["上火", "犯困", "失眠", "便秘", "咳嗽", "吃什么好", "调理", "症状", "食补", "补"],
}

# 意图锚定在哪类节点上。症状问法不能拿食材 id 去套 Symptom 模板。
INTENT_ANCHOR: dict[str, str] = {
    "pair": "Ingredient",
    "incompatible": "Ingredient",
    "contra_population": "Ingredient",
    "contra_item": "Ingredient",
    "nutrition": "Ingredient",
    "substitute": "Ingredient",
    "symptom_remedy": "Symptom",
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


def plan_queries(entities: list[dict[str, Any]], intents: list[str]) -> list[dict[str, str]]:
    """决定这次要跑哪些 (意图, 节点)。

    每个意图只绑一种节点：食材问法用 Ingredient 的 canonical_id，
    症状问法用 Symptom 的 canonical_id。对不上的组合直接跳过，
    避免拿食材 id 去匹配症状节点、查空还当成「图谱没有这条边」。
    """
    plans: list[dict[str, str]] = []
    seen: set[tuple[str, str]] = set()
    for intent in intents:
        anchor = INTENT_ANCHOR.get(intent)
        if not anchor or intent not in TEMPLATES:
            continue
        for entity in entities:
            if entity.get("label") != anchor:
                continue
            cid = entity.get("canonical_id") or ""
            if not cid:
                continue
            key = (intent, cid)
            if key in seen:
                continue
            seen.add(key)
            plans.append({"intent": intent, "canonical_id": cid, "label": anchor})
    return plans
