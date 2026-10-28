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
    with psycopg.connect(config.PG_DSN) as conn:
        cur = conn.cursor()
        sql = (
            "SELECT id, batch_id, node_type, canonical_id, name, props "
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


def merge_nodes(
    batch_id: str | None = None, batch_ids: list[str] | None = None
) -> int:
    """把 approved 节点候选 MERGE 进主图（按 canonical_id 幂等），返回合并数量。"""
    rows = _fetch_approved_nodes(batch_id, batch_ids)
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
                payload = [
                    {
                        "canonical_id": it["canonical_id"],
                        "props": {
                            k: v for k, v in (it["props"] or {}).items() if k in allowed
                        },
                    }
                    for it in items
                ]
                # label 不能参数化，只能拼串 —— 所以上面必须用白名单卡住
                #
                # .consume() 不能省：s.run() 返回的是**惰性结果**，auto-commit 的
                # 写事务要到结果被读完才提交。不消费就往下走，会话结束时驱动发的
                # RESET 会把这笔还没提交的写**回滚掉**，而 PG 侧照样把候选标成
                # merged —— 于是「库里 1331 个食材都是 merged，图里只有 888 个」，
                # 479 个食材连同营养边一起消失，还查不出错（2026-10-28 修）。
                s.run(
                    f"UNWIND $rows AS row "
                    f"MERGE (n:`{label}` {{canonical_id: row.canonical_id}}) "
                    f"SET n += row.props",
                    rows=payload,
                ).consume()
                merged_ids.extend(it["id"] for it in items)
    finally:
        driver.close()

    if merged_ids:
        with psycopg.connect(config.PG_DSN) as conn:
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
    with psycopg.connect(config.PG_DSN) as conn:
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
    """
    rows = _fetch_approved(batch_id, batch_ids)
    if not rows:
        log.info("没有待合并的 approved 候选")
        return 0

    # 过滤：目标节点 UNRESOLVED 的不合并（需人工补全）
    valid = [r for r in rows if r["dst_id"] and r["dst_id"] != "UNRESOLVED"]
    skipped = len(rows) - len(valid)
    if skipped:
        log.warning("跳过 %d 条 UNRESOLVED 目标节点", skipped)

    driver = _driver()
    merged_ids = []
    try:
        with driver.session() as s:
            # 逐条 MERGE（演示规模；生产可改为按 rel 分组后 UNWIND 批量）
            # 这里保持单 session 串行执行，天然避免并发竞态
            for r in valid:
                rel = r["rel"]
                if rel not in REL_LABEL:
                    log.warning("未知关系类型，跳过：%s", rel)
                    continue
                rk = relation_key(r["src_id"], rel, r["dst_id"])
                # amount / unit 只有营养边才有值。为 None 时不写这两句 ——
                # Cypher 里 SET r.x = null 等于**删掉属性**，会把别的来源
                # 早先写好的数值抹掉（MERGE 命中已有边时尤其危险）。
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
                s.run(
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
    finally:
        driver.close()

    # 回写候选表状态为 merged
    if merged_ids:
        with psycopg.connect(config.PG_DSN) as conn:
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
    with psycopg.connect(config.PG_DSN) as conn:
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
