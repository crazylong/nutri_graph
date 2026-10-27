"""
kg/subgraph.py —— 把「图查询的行」翻译成前端能直接画的子图（nodes + edges）

为什么单独一层：
  kg/query.py 返回的是**扁平的行**（cid / name / claim / elevel），那是为了喂给
  生成模型做上下文；前端要画图需要的是**点边结构**，两端要的东西不一样。
  与其在 api 层临时拼 JSON，不如在这里一次性把「行 → 子图」的映射定死，
  顺便把关系类型的中文名、意图→关系类型的对应也集中在一处
  （散在前端写一份、后端写一份，迟早对不上）。

两种输入：
  1. 问答链路的图结果（build_subgraph / build_facts）—— 不查库，纯翻译；
  2. 直接查 Neo4j 的邻域 / 两食材间路径（neighborhood / relation_between）——
     给「图谱探索」用，回答「这两个食材在图里是什么关系」。

输出格式（前端照着画）：
  nodes: [{id, label, name, anchor, hop, props:{...}}]
  edges: [{source, target, rel, rel_label, evidence_type, evidence_level,
           claim, detail, amount, unit, provisional, hop, path}]
"""
from __future__ import annotations

import logging
import re
from typing import Any

import config

log = logging.getLogger("kg.subgraph")


# ============================================================
# 关系类型 → 中文名（前端直接用，避免两边各维护一份枚举）
# ============================================================
REL_LABEL: dict[str, str] = {
    "CONTAINS_NUTRIENT": "含有",
    "RECOMMEND_PAIR": "推荐搭配",
    "INCOMPATIBLE_WITH": "不宜同食",
    "CONTRA_POPULATION": "人群禁忌",
    "SUBSTITUTE_FOR": "可替代",
    "CONTRA_ITEM": "禁忌事项",
    "HAS_SYMPTOM": "关联症状",
}
DEFAULT_RELS: list[str] = list(REL_LABEL)

# 意图 → 关系类型。问答链路的图结果里只有 intent（没有 type(r)），
# 因为每个模板查的就是一种关系，所以这里的映射是完备的。
INTENT_REL: dict[str, str] = {
    "pair": "RECOMMEND_PAIR",
    "incompatible": "INCOMPATIBLE_WITH",
    "contra_population": "CONTRA_POPULATION",
    "contra_item": "CONTRA_ITEM",
    "nutrition": "CONTAINS_NUTRIENT",
    "substitute": "SUBSTITUTE_FOR",
    # 症状问法查的是「某人群该避开哪些食材」，画出来的边是 食材→人群 的禁忌边
    "symptom_remedy": "CONTRA_POPULATION",
}

# 意图 → 对端节点标签（决定图上画成什么颜色/形状）
INTENT_TARGET_LABEL: dict[str, str] = {
    "pair": "Ingredient",
    "incompatible": "Ingredient",
    "contra_population": "Population",
    "contra_item": "Taboo",
    "nutrition": "Nutrient",
    "substitute": "Ingredient",
    "symptom_remedy": "Ingredient",
}


def _edge_detail(row: dict[str, Any]) -> str:
    """边上要显示的那句说明：效果 > 原因 > 依据 > 断言原文。"""
    for key in ("effect", "reason", "basis", "interaction", "claim"):
        v = row.get(key)
        if v:
            return str(v)
    return ""


# 数值回捞：补录链路（kg/promote.py）把营养值写进了 claim_text
# 「黑豆每 100g 含蛋白质 36.49 g」，关系上只留了 unit、没留 amount。
# 所以这里从断言文本里把最后一个「数字+单位」抠出来 ——
# 取最后一个是因为前面还有「每 100g」这个口径数字。
# 图里一旦真的存了 amount，这里就自动让位（见 _resolve_amount）。
_AMOUNT_RE = re.compile(
    r"(?P<num>\d+(?:\.\d+)?)\s*"
    r"(?P<unit>kcal|千卡|kj|g|mg|µg|μg|mcg|iu|%)",
    re.IGNORECASE,
)
_UNIT_ALIAS = {"μg": "µg", "mcg": "µg", "千卡": "kcal", "kj": "kJ", "iu": "IU"}


def parse_amount(text: str) -> tuple[float | None, str]:
    """从断言文本里回捞 (数值, 单位)。抓不到就 (None, '')。"""
    hit = None
    for hit in _AMOUNT_RE.finditer(text or ""):  # noqa: B007 —— 故意只留最后一次匹配
        pass
    if not hit:
        return None, ""
    unit = hit.group("unit")
    return float(hit.group("num")), _UNIT_ALIAS.get(unit, unit)


def _resolve_amount(row: dict[str, Any]) -> tuple[float | None, str]:
    """优先用关系上的 amount 属性，没有才回捞断言文本。"""
    amt = row.get("amount")
    if isinstance(amt, bool):
        amt = None
    if isinstance(amt, (int, float)):
        return float(amt), row.get("unit") or ""
    text = " ".join(
        str(row.get(k) or "")
        for k in ("claim", "claim_text", "detail", "effect", "reason", "basis")
    )
    num, unit = parse_amount(text)
    return num, unit or row.get("unit") or ""


def _mk_node(
    cid: str, label: str, name: str, *, anchor: bool = False, hop: int = 0, **props
) -> dict[str, Any]:
    clean = {k: v for k, v in props.items() if v not in (None, "")}
    return {
        "id": cid,
        "label": label or "Unknown",
        "name": name or cid,
        "anchor": bool(anchor),
        "hop": hop,
        "props": clean,
    }


def _mk_edge(
    source: str, target: str, rel: str, row: dict[str, Any], **extra
) -> dict[str, Any]:
    amount, unit = _resolve_amount(row)
    return {
        "source": source,
        "target": target,
        "rel": rel,
        "rel_label": REL_LABEL.get(rel, rel),
        "evidence_type": row.get("etype") or row.get("evidence_type") or "",
        "evidence_level": row.get("elevel") or row.get("evidence_level") or "",
        "claim": row.get("claim") or row.get("claim_text") or "",
        "detail": _edge_detail(row),
        "amount": amount,
        "unit": unit,
        "provisional": bool(row.get("provisional") or row.get("prov")),
        **extra,
    }


# ============================================================
# 1) 问答链路：图结果行 → 子图（不查库）
# ============================================================
def build_subgraph(
    entities: list[dict[str, Any]] | None,
    results: list[dict[str, Any]] | None,
) -> dict[str, Any] | None:
    """把 /chat 的 `_entities` + `_graph_results` 画成一张子图。

    entities 是锚点（用户问到的食材/症状），results 是它周围的一圈边。
    边的起点要认准是哪个锚点：一句话里可能有两个食材（「西红柿和牛肉能一起吃吗」），
    所以 graph_query_node 给每行打了 `_src_cid`，没有它时退化成第一个锚点。
    """
    entities = entities or []
    results = results or []
    if not results:
        return None

    nodes: dict[str, dict[str, Any]] = {}
    edges: list[dict[str, Any]] = []
    seen_edges: set[tuple[str, str, str]] = set()

    def add_node(cid, label, name, **kw):
        if not cid:
            return None
        if cid not in nodes:
            nodes[cid] = _mk_node(cid, label, name, **kw)
        elif kw.get("anchor"):
            nodes[cid]["anchor"] = True
        return cid

    anchors: list[str] = []
    for e in entities:
        if e.get("label") not in ("Ingredient", "Symptom", "Population"):
            continue
        cid = e.get("canonical_id")
        if not cid:
            continue
        add_node(cid, e.get("label"), e.get("matched_alias") or e.get("name") or cid,
                 anchor=True)
        if cid not in anchors:
            anchors.append(cid)

    for row in results:
        intent = row.get("intent", "")
        rel = INTENT_REL.get(intent)
        if not rel:
            continue

        src = row.get("_src_cid") or (anchors[0] if anchors else "")
        dst = row.get("cid") or row.get("canonical_id")
        if not src or not dst:
            continue

        # 起点节点：优先用行里带的 _src_name，没有就从锚点表里取
        src_node = nodes.get(src)
        if src_node is None:
            add_node(src, row.get("_src_label") or "Ingredient", row.get("_src_name") or src)
        dst_label = INTENT_TARGET_LABEL.get(intent, "Ingredient")
        add_node(dst, dst_label, row.get("name") or dst,
                 unit=row.get("unit") or "", taboo_type=row.get("ttype") or "")

        # 症状问法：补一条 人群 —关联症状→ 症状 的边，否则图上只看到一个孤立的人群。
        # 症状节点就是这次查询的锚点（src），不要另起一个 SYM:: 的影子节点。
        if intent == "symptom_remedy":
            pop_name = row.get("population") or ""
            sym_name = row.get("symptom") or ""
            if pop_name:
                add_node(f"POP::{pop_name}", "Population", pop_name)
                edges.append(_mk_edge(dst, f"POP::{pop_name}", "CONTRA_POPULATION", row))
                if sym_name:
                    edges.append(_mk_edge(f"POP::{pop_name}", src, "HAS_SYMPTOM", row))
            continue

        key = (src, rel, dst)
        if key in seen_edges:
            continue
        seen_edges.add(key)
        edges.append(_mk_edge(src, dst, rel, row))

    if not nodes:
        return None
    return {
        "nodes": list(nodes.values()),
        "edges": edges,
        "meta": {"source": "chat", "anchors": anchors},
    }


def build_facts(results: list[dict[str, Any]] | None) -> list[dict[str, Any]]:
    """图结果行 → 表格行。回答里那段自然语言的「依据」就来自同一批数据。

    为什么要单独给一份结构化的：让前端去正则抠「每 100g 含蛋白质 36.49 g」
    是在给自己挖坑 —— 模型换个说法表格就废了。表格吃的是字段，不是文本。
    """
    out: list[dict[str, Any]] = []
    for row in results or []:
        amount, unit = _resolve_amount(row)
        out.append(
            {
                "intent": row.get("intent", ""),
                "name": row.get("name") or row.get("cid") or "",
                "amount": amount,
                "unit": unit,
                "detail": _edge_detail(row),
                "claim": row.get("claim") or "",
                "evidence_type": row.get("etype") or "",
                "evidence_level": row.get("elevel") or "",
                "provisional": bool(row.get("provisional")),
                "population": row.get("population") or "",
                "symptom": row.get("symptom") or "",
                "target_id": row.get("cid") or "",
            }
        )
    return out


# ============================================================
# 2) 直接查图：邻域 / 两食材之间的关系
# ============================================================
_driver = None


def _get_driver():
    global _driver
    if _driver is None:
        from neo4j import GraphDatabase

        _driver = GraphDatabase.driver(
            config.NEO4J_URI, auth=(config.NEO4J_USER, config.NEO4J_PASSWORD)
        )
    return _driver


# 一跳邻域：把 (a)-[r]-(b) 的所有关系捞出来，方向用 startNode(r) 判定。
# 图上节点只打一个标签（见 scripts/load_seed.py），所以 labels(x)[0] 就是类型。
_NEIGHBOR_CYPHER = """
MATCH (a {canonical_id: $cid})-[r]-(b)
WHERE type(r) IN $rels
  AND ($include_provisional OR coalesce(r.provisional, false) = false)
RETURN labels(a)[0] AS a_label, a.canonical_id AS a_id,
       coalesce(a.canonical_name, a.name) AS a_name,
       labels(b)[0] AS b_label, b.canonical_id AS b_id,
       coalesce(b.canonical_name, b.name) AS b_name,
       startNode(r) = a AS outward,
       type(r) AS rel, r.evidence_type AS etype, r.evidence_level AS elevel,
       r.claim_text AS claim, r.effect AS effect, r.reason AS reason,
       r.basis AS basis, r.amount AS amount, r.unit AS unit,
       coalesce(r.provisional, false) AS prov
LIMIT $limit
"""

# 两个食材之间的直接关系
_DIRECT_CYPHER = """
MATCH (a {canonical_id: $a})-[r]-(b {canonical_id: $b})
WHERE type(r) IN $rels
  AND ($include_provisional OR coalesce(r.provisional, false) = false)
RETURN startNode(r) = a AS outward,
       type(r) AS rel, r.evidence_type AS etype, r.evidence_level AS elevel,
       r.claim_text AS claim, r.effect AS effect, r.reason AS reason,
       r.basis AS basis, r.amount AS amount, r.unit AS unit,
       coalesce(r.provisional, false) AS prov
LIMIT $limit
"""

# 没有直接关系时找最短路径（跨营养素/人群的间接关系也算「有关系」）
_PATH_CYPHER = """
MATCH p = shortestPath((a {canonical_id: $a})-[*1..%d]-(b {canonical_id: $b}))
WHERE all(r IN relationships(p) WHERE type(r) IN $rels
      AND ($include_provisional OR coalesce(r.provisional, false) = false))
RETURN [n IN nodes(p) | {id: n.canonical_id, label: labels(n)[0],
        name: coalesce(n.canonical_name, n.name)}] AS ns,
       [r IN relationships(p) | {rel: type(r), sid: startNode(r).canonical_id,
        tid: endNode(r).canonical_id, etype: r.evidence_type,
        elevel: r.evidence_level, claim: r.claim_text, effect: r.effect,
        reason: r.reason, basis: r.basis, amount: r.amount, unit: r.unit,
        prov: coalesce(r.provisional, false)}] AS rs
LIMIT $limit
"""

_NODE_CYPHER = """
MATCH (n {canonical_id: $cid})
RETURN labels(n)[0] AS label, n.canonical_id AS cid,
       coalesce(n.canonical_name, n.name) AS name
LIMIT 1
"""


def _run(cypher: str, **params) -> list[dict[str, Any]]:
    with _get_driver().session() as s:
        return [dict(r) for r in s.run(cypher, **params)]


def fetch_node(cid: str) -> dict[str, Any] | None:
    rows = _run(_NODE_CYPHER, cid=cid)
    return rows[0] if rows else None


def _merge(dst: dict[str, Any], src: dict[str, Any], hop: int) -> None:
    """把一份子图并进另一份，节点去重、边去重（同一对点同一种关系只留一条）。"""
    have_n = {n["id"] for n in dst["nodes"]}
    for n in src.get("nodes", []):
        if n["id"] not in have_n:
            node = dict(n)
            node["hop"] = hop if not node.get("anchor") else 0
            dst["nodes"].append(node)
            have_n.add(n["id"])
        elif n.get("anchor"):
            for x in dst["nodes"]:
                if x["id"] == n["id"]:
                    x["anchor"] = True

    have_e = {(e["source"], e["rel"], e["target"]) for e in dst["edges"]}
    for e in src.get("edges", []):
        key = (e["source"], e["rel"], e["target"])
        if key in have_e:
            continue
        have_e.add(key)
        edge = dict(e)
        edge["hop"] = hop
        dst["edges"].append(edge)


def neighborhood(
    cid: str,
    depth: int = 1,
    rels: list[str] | None = None,
    include_provisional: bool | None = None,
    max_nodes: int | None = None,
) -> dict[str, Any]:
    """某个节点的 N 跳邻域。depth=2 时只对一跳里的食材再扩一层（控制爆炸）。"""
    rels = rels or DEFAULT_RELS
    if include_provisional is None:
        include_provisional = config.INCLUDE_PROVISIONAL
    max_nodes = max_nodes or config.SUBGRAPH_MAX_NODES

    rows = _run(
        _NEIGHBOR_CYPHER,
        cid=cid,
        rels=rels,
        include_provisional=include_provisional,
        limit=config.SUBGRAPH_MAX_EDGES,
    )
    out = _rows_to_subgraph(rows, anchor_ids=[cid])

    if depth >= 2:
        seeds = [
            n["id"] for n in out["nodes"]
            if n["id"] != cid and n["label"] == "Ingredient"
        ][: config.SUBGRAPH_DEPTH2_SEEDS]
        for seed in seeds:
            sub_rows = _run(
                _NEIGHBOR_CYPHER,
                cid=seed,
                rels=rels,
                include_provisional=include_provisional,
                limit=max(4, config.SUBGRAPH_MAX_EDGES // max(len(seeds), 1)),
            )
            _merge(out, _rows_to_subgraph(sub_rows, anchor_ids=[seed]), hop=2)
            if len(out["nodes"]) >= max_nodes:
                break

    return _trim(out, max_nodes)


def relation_between(
    a_cid: str,
    b_cid: str,
    hops: int | None = None,
    rels: list[str] | None = None,
    include_provisional: bool | None = None,
    expand: bool = False,
) -> dict[str, Any]:
    """两个食材在图里是什么关系。

    先看有没有直接边（搭配/不宜同食/替代）；没有再找最短路径 ——
    「黑豆和菠菜都含铁」这种经营养素中转的关系，在食疗场景里正是要讲的东西。
    """
    rels = rels or DEFAULT_RELS
    hops = hops or config.SUBGRAPH_MAX_HOPS
    if include_provisional is None:
        include_provisional = config.INCLUDE_PROVISIONAL

    out: dict[str, Any] = {"nodes": [], "edges": [], "meta": {"hops": hops}}
    for cid in (a_cid, b_cid):
        n = fetch_node(cid)
        if n:
            out["nodes"].append(_mk_node(cid, n["label"], n["name"], anchor=True))

    rows = _run(
        _DIRECT_CYPHER,
        a=a_cid, b=b_cid, rels=rels,
        include_provisional=include_provisional,
        limit=config.SUBGRAPH_MAX_EDGES,
    )
    if rows:
        for r in rows:
            src, dst = (a_cid, b_cid) if r.get("outward") else (b_cid, a_cid)
            out["edges"].append(_mk_edge(src, dst, r["rel"], r, hop=1))
            _touch_node(out, dst)
        out["meta"]["kind"] = "direct"
        if expand:
            _merge(out, neighborhood(a_cid, 1, rels, include_provisional), hop=2)
            _merge(out, neighborhood(b_cid, 1, rels, include_provisional), hop=2)
        return _trim(out, config.SUBGRAPH_MAX_NODES)

    paths = _run(
        _PATH_CYPHER % max(1, min(int(hops), 4)),
        a=a_cid, b=b_cid, rels=rels,
        include_provisional=include_provisional,
        limit=3,
    )
    if paths:
        for p in paths:
            for n in p.get("ns") or []:
                if n.get("id") and n["id"] not in {x["id"] for x in out["nodes"]}:
                    out["nodes"].append(
                        _mk_node(n["id"], n.get("label"), n.get("name"),
                                 anchor=n["id"] in (a_cid, b_cid), hop=1)
                    )
            for r in p.get("rs") or []:
                out["edges"].append(
                    _mk_edge(r["sid"], r["tid"], r["rel"], r, hop=1, path=True)
                )
        out["meta"]["kind"] = "path"
    else:
        out["meta"]["kind"] = "none"

    if expand:
        _merge(out, neighborhood(a_cid, 1, rels, include_provisional), hop=2)
        _merge(out, neighborhood(b_cid, 1, rels, include_provisional), hop=2)
    return _trim(out, config.SUBGRAPH_MAX_NODES)


def _touch_node(out: dict[str, Any], cid: str) -> None:
    if any(n["id"] == cid for n in out["nodes"]):
        return
    n = fetch_node(cid)
    if n:
        out["nodes"].append(_mk_node(cid, n["label"], n["name"], hop=1))


def _rows_to_subgraph(
    rows: list[dict[str, Any]], anchor_ids: list[str] | None = None
) -> dict[str, Any]:
    """Cypher 返回的邻接行 → 子图。"""
    anchor_ids = anchor_ids or []
    nodes: dict[str, dict[str, Any]] = {}
    edges: list[dict[str, Any]] = []

    def put(cid, label, name, anchor=False):
        if not cid:
            return
        if cid not in nodes:
            nodes[cid] = _mk_node(cid, label, name, anchor=anchor, hop=0 if anchor else 1)
        elif anchor:
            nodes[cid]["anchor"] = True

    for r in rows:
        put(r.get("a_id"), r.get("a_label"), r.get("a_name"),
            anchor=r.get("a_id") in anchor_ids)
        put(r.get("b_id"), r.get("b_label"), r.get("b_name"),
            anchor=r.get("b_id") in anchor_ids)
        src, dst = (r.get("a_id"), r.get("b_id")) if r.get("outward") \
            else (r.get("b_id"), r.get("a_id"))
        if not src or not dst:
            continue
        edges.append(_mk_edge(src, dst, r.get("rel", ""), r, hop=1))

    return {"nodes": list(nodes.values()), "edges": edges, "meta": {}}


def _trim(g: dict[str, Any], max_nodes: int) -> dict[str, Any]:
    """超量时截断：锚点和 hop=1 优先保留，hop=2 的先丢。"""
    if len(g["nodes"]) <= max_nodes:
        return g
    keep = [n for n in g["nodes"] if n.get("anchor") or n.get("hop", 1) == 1]
    rest = [n for n in g["nodes"] if not (n.get("anchor") or n.get("hop", 1) == 1)]
    keep += rest[: max(0, max_nodes - len(keep))]
    ids = {n["id"] for n in keep}
    g["nodes"] = keep
    g["edges"] = [e for e in g["edges"] if e["source"] in ids and e["target"] in ids]
    g["meta"]["truncated"] = True
    return g


# ============================================================
# 3) 名字 → canonical_id（给「图谱探索」用）
# ============================================================
def resolve(term: str, prefer: str = "Ingredient") -> dict[str, Any] | None:
    """把一个词链到图上的节点。走的是问答同一条实体链接链路（字典 + 模糊）。

    没链上也返回一个带 near_misses 的壳（canonical_id 为空），
    让接口能回一句「你是不是想查 XX」，而不是干巴巴的 404。
    """
    from kg.normalize import EntityLinker, get_alias_store, normalize_text

    term = normalize_text(term or "")
    if not term:
        return None

    linker = EntityLinker(get_alias_store())
    res = linker.link_question(term)
    if not res.entities:
        res = linker.link(term)

    near = [
        {
            "candidate_id": n.get("candidate_id"),
            "candidate_alias": n.get("candidate_alias"),
        }
        for n in (res.near_misses or [])
    ][:3]

    ents = res.entities or []
    if not ents:
        return {
            "canonical_id": "", "label": "", "name": term,
            "method": "", "score": 0.0, "near_misses": near,
        }

    pick = next((e for e in ents if e.label == prefer), ents[0])
    return {
        "canonical_id": pick.canonical_id,
        "label": pick.label,
        "name": pick.matched_alias or pick.name,
        "method": pick.method,
        "score": round(float(pick.score or 0.0), 3),
        "near_misses": near,
    }
