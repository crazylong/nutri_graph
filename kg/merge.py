"""
kg/merge.py —— 主图**唯一**写入口（幂等）

设计要点：
  - 只消费 status='approved' 的候选（kg_node_candidate 节点 / kg_candidate 边）
  - 先合并节点，再合并边 —— 边依赖两端节点存在
  - UNWIND 批量，单线程（避免 MERGE 竞态产生重复边）
  - 关系用 relation_key 业务键做确定性 MERGE（Neo4j 无关系唯一约束）
  - 每条边打 batch_id，出问题可整批回滚

为什么节点也必须走这里：
  9.3 之前只有边候选表，新食材没有地方表达「要建一个 Ingredient 节点」，
  于是要么某个脚本偷偷写节点，要么手工 Cypher —— 两种都会让主图出现
  第二个写入口，从此「这条边是谁加的」查不清。节点候选就是为了把口子收回来。
"""
from __future__ import annotations

import logging
from collections import defaultdict
from typing import Any

import psycopg
from neo4j import GraphDatabase

import config
from kg.schema import relation_key, sources_to_json

import db

log = logging.getLogger("kg.merge")

# 关系类型 → (源 label, 目标 label)
REL_LABEL = {
    "CONTAINS_NUTRIENT": ("Ingredient", "Nutrient"),
    "RECOMMEND_PAIR": ("Ingredient", "Ingredient"),
    "INCOMPATIBLE_WITH": ("Ingredient", "Ingredient"),
    "CONTRA_POPULATION": ("Ingredient", "Population"),
    "SUBSTITUTE_FOR": ("Ingredient", "Ingredient"),
    "CONTRA_ITEM": ("Ingredient", "Taboo"),
    "HAS_SYMPTOM": ("Population", "Symptom"),
}

# 允许写入主图的节点属性白名单。
# 为什么必须白名单：props 来自外部抓取的产物（payload JSONB），
# 直接 `SET n += $props` 等于让外部数据随意往图里塞属性，
# 拼错一个键就多一个脏属性，而且很难事后清理。
# 图上已有节点时，普通导入不能改这些。重录可以改前三个，provenance 永远不改，
# 否则 rollback 会把这个食材连同搭配、禁忌一起当成「本批新建」删掉。
_STICKY_PROPS = {"summary", "provenance", "canonical_name", "category"}
_RERECORD_LOCKED = {"provenance"}


def match_props_for_existing(props: dict, *, re_record: bool) -> dict:
    """已有节点这次允许 SET 的属性。新建节点不走这里，白名单原样写入。"""
    locked = _RERECORD_LOCKED if re_record else _STICKY_PROPS
    return {k: v for k, v in props.items() if k not in locked}


NODE_PROPS_WHITELIST: dict[str, set[str]] = {
    "Ingredient": {
        "canonical_name", "category", "nature", "aliases", "summary",
        "scientific_name", "source_count", "provisional", "provenance",
    },
    # Nutrient 刻意不带 provenance：营养素有「共享词汇」的性质，
    # 蛋白质/能量会被所有食材引用。如果给它打批次标记，
    # 回滚某一批补录时会把别的食材也在用的营养素节点一起删掉。
    # 代价是回滚后可能留下没有边的孤立营养素节点 —— 比误删安全得多。
    "Nutrient": {"name", "unit", "category", "provisional"},
}


def _driver():
    return GraphDatabase.driver(
        config.NEO4J_URI, auth=(config.NEO4J_USER, config.NEO4J_PASSWORD)
    )


# ============================================================
# 节点
# ============================================================
def _fetch_approved_nodes(
    batch_id: str | None = None, batch_ids: list[str] | None = None
) -> list[dict[str, Any]]:
    with db.connect() as conn:
        cur = conn.cursor()
        sql = (
            "SELECT id, batch_id, node_type, canonical_id, name, props, re_record "
            "FROM kg_node_candidate WHERE status = 'approved'"
        )
        if batch_ids is not None:
            cur.execute(sql + " AND batch_id = ANY(%s)", (batch_ids,))
        elif batch_id:
            cur.execute(sql + " AND batch_id = %s", (batch_id,))
        else:
            cur.execute(sql)
        cols = [d[0] for d in cur.description]
        return [dict(zip(cols, row)) for row in cur.fetchall()]


def _rerecord_ingredient_ids(
    batch_id: str | None = None, batch_ids: list[str] | None = None
) -> list[str]:
    """本批要替换营养边的食材。merge_nodes 已经把候选标成 merged，不能只看 approved。"""
    with db.connect() as conn:
        cur = conn.cursor()
        sql = (
            "SELECT canonical_id FROM kg_node_candidate "
            "WHERE node_type = 'Ingredient' AND re_record = true"
        )
        if batch_ids is not None:
            cur.execute(sql + " AND batch_id = ANY(%s)", (batch_ids,))
        elif batch_id:
            cur.execute(sql + " AND batch_id = %s", (batch_id,))
        else:
            cur.execute(sql + " AND status = 'approved'")
        return [row[0] for row in cur.fetchall() if row[0]]


def _node_text_hits(row: dict) -> list:
    """名称、别名、性味、简介都要扫。props 里的名称键是 canonical_name，不是 name。"""
    from kg.sensitive import scan

    props = row.get("props") or {}
    if not isinstance(props, dict):
        props = {}
    hits = []
    if row.get("name"):
        hits.extend(scan(str(row["name"]), field_name="name"))
    for key in ("canonical_name", "nature", "summary", "scientific_name"):
        val = props.get(key)
        if val:
            hits.extend(scan(str(val), field_name=key))
    aliases = props.get("aliases") or []
    if isinstance(aliases, (list, tuple)):
        text = "、".join(str(a) for a in aliases if a)
    else:
        text = str(aliases or "")
    if text:
        hits.extend(scan(text, field_name="aliases"))
    return hits


def _nodes_passing_sensitive(rows: list[dict]) -> list[dict]:
    """block 的节点不进主图，状态保持 approved，也不记进 merged。review 仍合并。"""
    from kg.sensitive import log_hits, term_hash

    kept = []
    for row in rows:
        hits = _node_text_hits(row)
        if hits:
            log_hits("review", hits, source="merge_nodes")
        blocking = [h for h in hits if h.action == "block"]
        if blocking:
            h0 = blocking[0]
            log.error(
                "节点候选 %s 命中 %s 级敏感词（hash=%s），拒绝合并",
                row.get("id"), h0.category, term_hash(h0.word),
            )
            continue
        kept.append(row)
    return kept


def merge_nodes(
    batch_id: str | None = None, batch_ids: list[str] | None = None
) -> int:
    """把 approved 节点候选 MERGE 进主图（按 canonical_id 幂等），返回合并数量。"""
    rows = _fetch_approved_nodes(batch_id, batch_ids)
    if not rows:
        log.info("没有待合并的节点候选")
        return 0
    rows = _nodes_passing_sensitive(rows)
    if not rows:
        log.info("没有待合并的节点候选")
        return 0

    groups: dict[str, list[dict]] = defaultdict(list)
    for r in rows:
        groups[r["node_type"]].append(r)

    merged_ids: list[int] = []
    driver = _driver()
    try:
        with driver.session() as s:
            for label, items in groups.items():
                allowed = NODE_PROPS_WHITELIST.get(label)
                if allowed is None:
                    log.warning("未知节点类型，跳过：%s", label)
                    continue
                payload = []
                for it in items:
                    props = {
                        k: v for k, v in (it["props"] or {}).items() if k in allowed
                    }
                    payload.append({
                        "canonical_id": it["canonical_id"],
                        "create_props": props,
                        "match_props": (
                            match_props_for_existing(props, re_record=bool(it.get("re_record")))
                            if label == "Ingredient"
                            else props
                        ),
                    })
                # label 不能参数化，只能拼串 —— 所以上面必须用白名单卡住
                #
                # .consume() 不能省：s.run() 返回的是**惰性结果**，auto-commit 的
                # 写事务要到结果被读完才提交。不消费就往下走，会话结束时驱动发的
                # RESET 会把这笔还没提交的写**回滚掉**，而 PG 侧照样把候选标成
                # merged —— 于是「库里 1331 个食材都是 merged，图里只有 888 个」，
                # 479 个食材连同营养边一起消失，还查不出错（2026-10-28 修）。
                #
                # ON CREATE 写全量；ON MATCH 按重录标记裁掉粘性字段。
                # 种子没有候选行，第一次普通导入如果走 SET n += props，会把图上的名称改掉。
                s.run(
                    f"UNWIND $rows AS row "
                    f"MERGE (n:`{label}` {{canonical_id: row.canonical_id}}) "
                    f"ON CREATE SET n += row.create_props "
                    f"ON MATCH SET n += row.match_props",
                    rows=payload,
                ).consume()
                merged_ids.extend(it["id"] for it in items)
    finally:
        driver.close()

    if merged_ids:
        with db.connect() as conn:
            conn.execute(
                "UPDATE kg_node_candidate SET status = 'merged' WHERE id = ANY(%s)",
                (merged_ids,),
            )
    log.info("节点合并完成：%d 个", len(merged_ids))
    return len(merged_ids)


def _fetch_approved(
    batch_id: str | None = None, batch_ids: list[str] | None = None
) -> list[dict[str, Any]]:
    """从候选表取待合并的 approved 边候选。"""
    cols = (
        "id, batch_id, src_id, rel, dst_id, claim_text, evidence_type, "
        "evidence_level, sources, confidence, source_locator, provisional, source_count, "
        "amount, unit"
    )
    with db.connect() as conn:
        cur = conn.cursor()
        if batch_ids is not None:
            cur.execute(
                f"SELECT {cols} FROM kg_candidate "
                "WHERE status = 'approved' AND batch_id = ANY(%s)",
                (batch_ids,),
            )
        elif batch_id:
            cur.execute(
                f"SELECT {cols} FROM kg_candidate "
                "WHERE status = 'approved' AND batch_id = %s",
                (batch_id,),
            )
        else:
            cur.execute(f"SELECT {cols} FROM kg_candidate WHERE status = 'approved'")
        names = [d[0] for d in cur.description]
        return [dict(zip(names, row)) for row in cur.fetchall()]


def merge(
    batch_id: str | None = None, batch_ids: list[str] | None = None
) -> int:
    """把 approved 边候选 MERGE 进主图，返回合并数量。

    只处理边。节点请先跑 merge_nodes —— promote 里就是「先节点后边」。
    重录批次会先删掉这些食材的 CONTAINS_NUTRIENT，再写本批新边，两步在同一个事务里。
    """
    rerecord_ids = _rerecord_ingredient_ids(batch_id, batch_ids)
    rows = _fetch_approved(batch_id, batch_ids)
    if not rows and not rerecord_ids:
        log.info("没有待合并的 approved 候选")
        return 0

    # 过滤：目标节点 UNRESOLVED 的不合并（需人工补全）
    valid = [r for r in rows if r["dst_id"] and r["dst_id"] != "UNRESOLVED"]
    skipped = len(rows) - len(valid)
    if skipped:
        log.warning("跳过 %d 条 UNRESOLVED 目标节点", skipped)

    # 最后一道敏感词闸（步骤 2.5）：防"批准之后词表更新了"或有人直连 PG 改了 status。
    # 这里读得到的是边的 claim_text —— source_desc / note 不在这份结果里，
    # 那两个字段在 bundle 批准时扫（见 kg.sensitive.assert_bundle_approvable）。
    from kg.sensitive import log_hits, scan, term_hash

    clean = []
    for r in valid:
        hits = scan(str(r.get("claim_text") or ""), field_name="claim_text")
        if not hits:
            clean.append(r)
            continue
        log_hits("review", hits, source="merge")
        blocking = [h for h in hits if h.action == "block"]
        if blocking:
            h0 = blocking[0]
            log.error(
                "边候选 %s 命中 %s 级敏感词（hash=%s），拒绝合并",
                r.get("id"), h0.category, term_hash(h0.word),
            )
        else:
            clean.append(r)
    if len(clean) != len(valid):
        log.warning("敏感词闸拦下 %d 条边候选", len(valid) - len(clean))
    valid = clean

    driver = _driver()
    merged_ids = []
    try:
        with driver.session() as s:
            with s.begin_transaction() as tx:
                if rerecord_ids:
                    tx.run(
                        "UNWIND $cids AS cid "
                        "MATCH (:Ingredient {canonical_id: cid})-[r:CONTAINS_NUTRIENT]->() "
                        "DELETE r",
                        cids=rerecord_ids,
                    ).consume()
                for r in valid:
                    rel = r["rel"]
                    if rel not in REL_LABEL:
                        log.warning("未知关系类型，跳过：%s", rel)
                        continue
                    rk = relation_key(r["src_id"], rel, r["dst_id"])
                    amount = r.get("amount")
                    numeric = (
                        f"r.amount = $amount, r.unit = $unit, " if amount is not None else ""
                    )
                    cypher = (
                        f"MATCH (a {{canonical_id: $src_id}}) "
                        f"MATCH (b {{canonical_id: $dst_id}}) "
                        f"MERGE (a)-[r:{rel} {{relation_key: $relation_key}}]->(b) "
                        f"SET r.evidence_type = $evidence_type, "
                        f"r.evidence_level = $evidence_level, "
                        f"r.claim_text = $claim_text, "
                        f"r.sources = $sources, "
                        f"r.confidence = $confidence, "
                        f"r.source_locator = $source_locator, "
                        f"r.source_count = $source_count, "
                        f"r.provisional = $provisional, "
                        f"{numeric}"
                        f"r.batch_id = $batch_id, "
                        f"r.review_status = 'merged'"
                    )
                    tx.run(
                        cypher,
                        src_id=r["src_id"],
                        dst_id=r["dst_id"],
                        relation_key=rk,
                        evidence_type=r["evidence_type"],
                        evidence_level=r["evidence_level"],
                        claim_text=r["claim_text"],
                        sources=sources_to_json(r["sources"]),
                        confidence=float(r["confidence"] or 0.0),
                        source_locator=r.get("source_locator") or "",
                        source_count=int(r.get("source_count") or 0),
                        provisional=bool(r.get("provisional")),
                        amount=amount,
                        unit=r.get("unit") or "",
                        batch_id=r["batch_id"],
                    ).consume()
                    merged_ids.append(r["id"])
                tx.commit()
    finally:
        driver.close()

    # 回写候选表状态为 merged
    if merged_ids:
        with db.connect() as conn:
            conn.execute(
                "UPDATE kg_candidate SET status = 'merged' WHERE id = ANY(%s)",
                (merged_ids,),
            )
    log.info("合并完成：%d 条（跳过 %d 条 UNRESOLVED）", len(merged_ids), skipped)
    return len(merged_ids)


def rollback(batch_id: str) -> int:
    """按 batch_id 回滚（删该批次写入的边 + 该批次**新建**的节点）。

    只删「这个批次建的」节点，用 n.provenance 判定。
    为什么不能直接按候选表里的 canonical_id 删：审核时可能把新食材
    挂到了既有节点（黑豆 → 已有的黑大豆），那个节点是种子里就有的，
    删掉等于把别人的数据一起清了。

    provenance 必须**精确匹配**，不能用 CONTAINS：
    `provenance CONTAINS 'bundle:12'` 会同时命中 `bundle:120` / `bundle:1200`…
    —— 回滚一份 bundle:12，把后面一百多份数据一起删了，而且不报错。
    这不是理论风险：2026-10-28 线上真被删掉 120 个食材节点（测试用例里
    rollback("bundle:12") 打到了生产图），补回来花了一轮。
    provenance 的格式是 `bundle:<id> <source_key>:<external_id>`，所以
    「等于」或「以 `bundle:<id> ` 开头」（带空格）才是只属于这一份。
    """
    driver = _driver()
    deleted_nodes = 0
    try:
        with driver.session() as s:
            result = s.run(
                "MATCH ()-[r]->() WHERE r.batch_id = $bid DELETE r RETURN count(r) AS n",
                bid=batch_id,
            ).single()
            n = result["n"] if result else 0

            result = s.run(
                "MATCH (n) WHERE n.provenance = $bid "
                "   OR n.provenance STARTS WITH $bid_prefix "
                "DETACH DELETE n RETURN count(n) AS c",
                bid=batch_id, bid_prefix=f"{batch_id} ",
            ).single()
            deleted_nodes = result["c"] if result else 0
    finally:
        driver.close()
    with db.connect() as conn:
        conn.execute(
            "UPDATE kg_candidate SET status = 'pending' "
            "WHERE batch_id = %s AND status = 'merged'",
            (batch_id,),
        )
        conn.execute(
            "UPDATE kg_node_candidate SET status = 'pending' "
            "WHERE batch_id = %s AND status = 'merged'",
            (batch_id,),
        )
    log.info("回滚完成：删除 %d 条边、%d 个本批次新建的节点", n, deleted_nodes)
    return n


if __name__ == "__main__":
    import argparse

    logging.basicConfig(level=logging.INFO)
    p = argparse.ArgumentParser()
    p.add_argument("--batch-id", default=None, help="只合并指定批次")
    p.add_argument("--rollback", default=None, help="按 batch_id 回滚")
    args = p.parse_args()
    if args.rollback:
        rollback(args.rollback)
    else:
        merge_nodes(args.batch_id)
        merge(args.batch_id)
