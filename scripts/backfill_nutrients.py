"""
scripts/backfill_nutrients.py —— 给已经进主图的食材补上「后来才收录」的营养素边

为什么需要这个脚本：
  词表扩容（NUT_00023~00034）之后，新导入的数据会自动带上这些边；
  但**已经 merged 的 1330 条抓取数据不会** —— import_fcd 的幂等逻辑按
  「已 merged 的 food_key 跳过」，而且当时的营养值压根没落库（resolve 返回 None
  就被丢掉了）。所以只能回抓取包里再读一遍，只补差的那几条边。

三条硬约束（都是这个库的既有规矩，别为了省事绕开）：

  1. **走 kg_candidate → kg.merge 这一个写入口**，不直接 Cypher 写边。
     这样每条补出来的边都有 batch_id，出问题能整批回滚（--rollback）。
  2. **只补图上没有的边**。已有数值一律不覆盖 —— 那是审过的数据，
     要改得走新一轮审核，不能借「补词表」的机会悄悄重写。
  3. **幂等**。同一 batch 重跑：候选表 ON CONFLICT 更新，Neo4j 侧是 MERGE。

用法：
  python -m scripts.backfill_nutrients --dry-run     # 只算差多少，不写库
  python -m scripts.backfill_nutrients               # 补边 + 合并进主图
  python -m scripts.backfill_nutrients --rollback    # 撤销本批补的边
  python -m scripts.backfill_nutrients --keys phosphorus_mg,vitamin_b1_mg   # 只补这几项
"""
from __future__ import annotations

import argparse
import datetime as dt
import json
import logging
from collections import defaultdict
from pathlib import Path

import psycopg
from psycopg.types.json import Jsonb

import config
from kg import importer, merge as merge_mod, nutrients as nut_mod
from scripts import import_fcd

log = logging.getLogger("scripts.backfill_nutrients")

SOURCE_KEY = import_fcd.SOURCE_KEY  # fcd_crawler
DEFAULT_BATCH = "backfill:nutrients:" + dt.date.today().strftime("%Y%m%d")

# 一次查多少条食材的已有边。Neo4j 的 IN 列表不宜太大，500 稳。
_CHUNK = 500


def _value(raw) -> float | None:
    """抓取包里是 float，转成模板行之后也可能仍是数字。

    转不动就返回 None：None 的语义是「没有这个数」，不是 0。
    """
    try:
        return float(raw)
    except (TypeError, ValueError):
        return None


# ============================================================
# 读：抓取包 + 已进主图的 bundle
# ============================================================
def load_items(path: Path) -> dict[str, dict]:
    blob = json.loads(path.read_text(encoding="utf-8"))
    out: dict[str, dict] = {}
    for it in blob.get("items") or []:
        key = importer.name_key(it.get("name"))
        if key and key not in out:
            out[key] = it
    return out


def fetch_bundles() -> list[dict]:
    with psycopg.connect(config.PG_DSN) as conn:
        cur = conn.cursor()
        cur.execute(
            "SELECT id, food_key, canonical_name, canonical_id, source_count, "
            "       score_total, sources, evidence_type "
            "FROM kg_food_bundle "
            "WHERE source_key = %s AND status = 'merged' AND canonical_id <> '' "
            "ORDER BY id",
            (SOURCE_KEY,),
        )
        cols = [d[0] for d in cur.description]
        return [dict(zip(cols, r)) for r in cur.fetchall()]


def existing_edges(cids: list[str]) -> set[tuple[str, str]]:
    """图上已有的 (食材 cid, 营养素 cid) 组合。"""
    from kg.subgraph import _get_driver

    out: set[tuple[str, str]] = set()
    driver = _get_driver()
    for i in range(0, len(cids), _CHUNK):
        part = cids[i : i + _CHUNK]
        with driver.session() as s:
            rows = s.run(
                "MATCH (a:Ingredient)-[:CONTAINS_NUTRIENT]->(n:Nutrient) "
                "WHERE a.canonical_id IN $cids "
                "RETURN a.canonical_id AS cid, n.canonical_id AS nid",
                cids=part,
            )
            out.update((r["cid"], r["nid"]) for r in rows)
    return out


def graph_cids(cids: list[str]) -> tuple[set[str], set[str]]:
    """返回 (图里作为 Ingredient 存在的, 图里带任何 label 存在的)。

    第二个集合只用于报错：如果某个 cid 以别的 label 存在，说明身份判定出过岔子，
    那不是「漏写节点」，硬补会造出重复节点，得人工看。
    """
    from kg.subgraph import _get_driver

    as_ing: set[str] = set()
    any_label: set[str] = set()
    driver = _get_driver()
    for i in range(0, len(cids), _CHUNK):
        part = cids[i : i + _CHUNK]
        with driver.session() as s:
            rows = s.run(
                "MATCH (n) WHERE n.canonical_id IN $cids "
                "RETURN n.canonical_id AS cid, labels(n) AS labels",
                cids=part,
            )
            for r in rows:
                any_label.add(r["cid"])
                if "Ingredient" in (r["labels"] or []):
                    as_ing.add(r["cid"])
    return as_ing, any_label


def restore_node(cid: str, dry_run: bool = False) -> int:
    """按候选表里残留的审计记录，把某个丢失的食材节点连同它的边复原。

    为什么会有「节点没了但候选还在」：早期 `rollback()` 用
    `provenance CONTAINS $batch` 匹配，回滚 bundle:12 会连带删掉 bundle:120/129/1200…；
    测试用例跑清理时又对着同一张图删过，连 `kg_food_bundle` / `kg_node_candidate`
    的 PG 行都可能被按 `id` 误删（测试库的 id 很小，正好和生产撞上）。

    能复原的前提：**kg_candidate 里这条食材的边还在**。边的 claim_text / amount /
    evidence_level 都是审核时的原样，所以复原出来的数据和当初进图的一致，
    不是重新推导。唯一需要重建的是节点属性，只能从 dict_alias 的别名反推。
    """
    with psycopg.connect(config.PG_DSN) as conn:
        edges = conn.execute(
            "SELECT batch_id, rel, dst_id, claim_text, source_locator, evidence_type, "
            "       evidence_level, sources, confidence, provisional, source_count "
            "FROM kg_candidate WHERE src_id = %s ORDER BY id",
            (cid,),
        ).fetchall()
        aliases = [
            r[0]
            for r in conn.execute(
                "SELECT alias FROM dict_alias "
                "WHERE entity_type = 'Ingredient' AND canonical_id = %s ORDER BY alias",
                (cid,),
            ).fetchall()
        ]
        node = conn.execute(
            "SELECT status, props FROM kg_node_candidate "
            "WHERE node_type = 'Ingredient' AND canonical_id = %s",
            (cid,),
        ).fetchone()

    if not edges:
        print(f"{cid} 在 kg_candidate 里没有任何边，无法复原（没有可依据的审计记录）")
        return 0

    batch = edges[0][0]
    # 中文别名优先作为规范名；都没有就退回 cid（宁可显示难看，也不要编一个名字）
    name = next(
        (a for a in aliases if any("\u4e00" <= ch <= "\u9fff" for ch in a)),
        aliases[0] if aliases else cid,
    )
    print(f"{cid}：别名 {aliases}，待复原 {len(edges)} 条边（批次 {batch}）")
    if node and str((node[0] or "")).lower() == "merged":
        print("  节点候选已是 merged 但图里没有 → 改回 approved 让它重新合并")
    if dry_run:
        print("--dry-run：没有写库。")
        return len(edges)

    props = {
        "canonical_name": name,
        "aliases": aliases or [name],
        "summary": f"{name}",
        "provenance": f"{batch} restore:{cid}",
        "provisional": True,
    }
    with psycopg.connect(config.PG_DSN) as conn:
        conn.execute(
            """
            INSERT INTO kg_node_candidate
                (batch_id, node_type, canonical_id, name, props, aliases, bundle_id,
                 status, reviewer, reviewed_at)
            VALUES (%s, 'Ingredient', %s, %s, %s, %s, NULL, 'approved', 'restore', now())
            ON CONFLICT (canonical_id) DO UPDATE SET
                props = EXCLUDED.props,
                status = 'approved',
                reviewer = 'restore',
                reviewed_at = now()
            """,
            (batch, cid, name, Jsonb(props), aliases or [name]),
        )
        conn.execute(
            "UPDATE kg_candidate SET status = 'approved' WHERE src_id = %s", (cid,)
        )

    print(f"  节点 {merge_mod.merge_nodes(batch)} 个 / 边 {merge_mod.merge(batch)} 条")
    missing = [c for c in [cid] if c not in graph_cids([cid])[0]]
    print("  ✅ 已回到图中" if not missing else "  ❌ 仍不在图中，需人工看")
    return len(edges)


def repair_nodes(dry_run: bool = False) -> int:
    """把「PG 里标成 merged、图里其实没有」的食材节点补进图。

    这是 kg.merge.merge_nodes 缺 .consume() 留下的历史坑（写事务没提交就被 RESET
    回滚，PG 侧却已经标 merged）。补的时候不重新推导任何数据，只是让 merge_nodes
    把他们再跑一遍 —— 属性、别名都取自候选表里原来那份，和当初审核通过时一致。
    """
    with psycopg.connect(config.PG_DSN) as conn:
        rows = conn.execute(
            "SELECT canonical_id FROM kg_node_candidate "
            "WHERE node_type = 'Ingredient' AND status = 'merged' "
            "ORDER BY canonical_id"
        ).fetchall()
    merged = [r[0] for r in rows]
    as_ing, any_label = graph_cids(merged)
    missing = [c for c in merged if c not in as_ing]
    odd = [c for c in missing if c in any_label]
    print(
        f"候选 merged {len(merged)} 个 / 图里存在 {len(as_ing)} 个 / "
        f"缺失 {len(missing)} 个"
    )
    if odd:
        print(f"  其中 {len(odd)} 个以别的 label 存在，需人工确认：{odd[:5]}")
    if not missing:
        return 0
    if dry_run:
        print(f"  --dry-run：本应补 {len(missing)} 个，例如 {missing[:5]}")
        return len(missing)

    with psycopg.connect(config.PG_DSN) as conn:
        conn.execute(
            "UPDATE kg_node_candidate SET status = 'approved' "
            "WHERE node_type = 'Ingredient' AND canonical_id = ANY(%s) "
            "  AND status = 'merged'",
            (missing,),
        )
    n = merge_mod.merge_nodes()
    print(f"  merge_nodes 处理 {n} 个")
    as_ing2, _ = graph_cids(missing)
    left = [c for c in missing if c not in as_ing2]
    print(f"  补完后仍缺 {len(left)} 个" + (f"：{left[:5]}" if left else ""))
    return len(missing)


# ============================================================
# 算：哪些边缺
# ============================================================
def build_plan(
    bundles: list[dict], items: dict[str, dict], only_keys: set[str] | None = None
) -> tuple[list[dict], dict[str, int]]:
    """产出待补的边（还没有去重查图）。返回 (计划, 统计)。"""
    stats: dict[str, int] = defaultdict(int)
    plan: list[dict] = []

    for b in bundles:
        item = items.get(b["food_key"])
        if item is None:
            stats["bundle_without_source"] += 1
            continue
        stats["bundle_matched"] += 1
        vals = import_fcd.to_row(item)
        for col in importer.NUTRIENT_COLUMNS:
            if only_keys and col.key not in only_keys:
                continue
            raw = vals.get(col.key)
            if raw in (None, ""):
                continue
            value = _value(raw)
            if value is None:
                log.warning("「%s」的值 %r 不是数字，跳过", col.key, raw)
                stats["bad_value"] += 1
                continue
            label = importer.NUTRIENT_LABELS.get(col.key, col.key)
            ref = nut_mod.resolve(label, col.unit)
            if ref is None:
                # 走到这里说明词表/代码漂移了（CSV 有、resolve 没有），必须报出来
                log.warning("营养素「%s %s」解析不到节点，跳过", label, col.unit)
                stats["unresolved"] += 1
                continue
            if ref.canonical_id != col.nutrient_id:
                log.warning(
                    "「%s」模板写的是 %s，resolver 认的是 %s —— 以 resolver 为准",
                    label, col.nutrient_id, ref.canonical_id,
                )
            plan.append(
                {
                    "bundle_id": b["id"],
                    "src_id": b["canonical_id"],
                    "name": b["canonical_name"],
                    "ref": ref,
                    "value": value,
                    "col_key": col.key,
                    "source_count": int(b["source_count"] or 1),
                    "score_total": float(b["score_total"] or 0.8),
                    "sources": b["sources"] or [],
                    "evidence_type": b["evidence_type"] or "measured",
                }
            )
    return plan, dict(stats)


# ============================================================
# 写：候选 → 主图
# ============================================================
def stage(plan: list[dict], batch_id: str) -> tuple[int, int]:
    """写候选表。返回 (边候选数, 新建营养素节点候选数)。"""
    nodes: dict[str, object] = {p["ref"].canonical_id: p["ref"] for p in plan}

    with psycopg.connect(config.PG_DSN) as conn:
        # 营养素节点候选：DO NOTHING，既有节点（含种子装的）一律不碰。
        # 营养素有「共享词汇」性质，改它等于改所有引用它的食材。
        for cid, ref in nodes.items():
            conn.execute(
                """
                INSERT INTO kg_node_candidate
                    (batch_id, node_type, canonical_id, name, props, aliases, bundle_id,
                     status, reviewer, reviewed_at)
                VALUES (%s, 'Nutrient', %s, %s, %s, '{}', NULL, 'approved', %s, now())
                ON CONFLICT (canonical_id) DO NOTHING
                """,
                (
                    batch_id, cid, ref.name,
                    Jsonb({"name": ref.name, "unit": ref.unit, "category": ref.category}),
                    "backfill",
                ),
            )

        for p in plan:
            ref = p["ref"]
            prov = p["source_count"] < config.FOOD_TRUSTED_MIN_SOURCES
            level = (
                config.PROVISIONAL_EVIDENCE_LEVEL if prov
                else config.TRUSTED_EVIDENCE_LEVEL
            )
            claim = f"{p['name']}每 100g 含{ref.name} {p['value']:g} {ref.unit}"
            conn.execute(
                """
                INSERT INTO kg_candidate
                    (batch_id, src_type, src_id, rel, dst_type, dst_id, dst_name,
                     claim_text, evidence_type, evidence_level, sources, confidence,
                     source_locator, provisional, source_count, amount, unit,
                     status, reviewer, reviewed_at)
                VALUES (%s,'Ingredient',%s,'CONTAINS_NUTRIENT','Nutrient',%s,%s,
                        %s,%s,%s,%s,%s,%s,%s,%s,%s,%s,'approved','backfill', now())
                ON CONFLICT (batch_id, src_id, rel, dst_id) DO UPDATE SET
                    claim_text = EXCLUDED.claim_text,
                    evidence_level = EXCLUDED.evidence_level,
                    amount = EXCLUDED.amount,
                    unit = EXCLUDED.unit,
                    status = 'approved',
                    reviewed_at = now()
                """,
                (
                    batch_id, p["src_id"], ref.canonical_id, ref.name,
                    claim, p["evidence_type"], level,
                    Jsonb(p["sources"]), min(1.0, p["score_total"]),
                    f"backfill:bundle{p['bundle_id']}",
                    prov, p["source_count"], p["value"], ref.unit,
                ),
            )
    return len(plan), len(nodes)


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="给已进主图的食材补新收录的营养素边")
    ap.add_argument("--file", type=Path, default=None, help="foods.json 路径")
    ap.add_argument("--batch", default=DEFAULT_BATCH, help="批次号（回滚时用）")
    ap.add_argument("--keys", default="", help="只补这些列 key，逗号分隔")
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--rollback", action="store_true")
    ap.add_argument(
        "--repair-nodes", action="store_true",
        help="只修「候选已 merged、图里没有」的食材节点（历史遗留），不补边",
    )
    ap.add_argument(
        "--restore", default="",
        help="按候选表里的审计记录复原某个丢失的食材节点，如 --restore ING_01075",
    )
    args = ap.parse_args(argv)

    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s %(message)s")

    if args.restore:
        restore_node(args.restore, dry_run=args.dry_run)
        return 0

    if args.repair_nodes:
        repair_nodes(dry_run=args.dry_run)
        return 0

    if args.rollback:
        n = merge_mod.rollback(args.batch)
        # 候选表状态由 rollback 复位为 pending，这里再标成 rejected，
        # 否则下一次 merge_pending() 会把它们又捞一遍。
        with psycopg.connect(config.PG_DSN) as conn:
            conn.execute(
                "UPDATE kg_candidate SET status = 'rejected' WHERE batch_id = %s",
                (args.batch,),
            )
        print(f"已回滚批次 {args.batch}：删除 {n} 条边")
        return 0

    path = args.file or import_fcd._default_file()
    if not path.exists():
        print(f"找不到抓取包：{path}")
        return 2

    only = {k.strip() for k in args.keys.split(",") if k.strip()} or None
    items = load_items(path)
    bundles = fetch_bundles()
    print(f"抓取包 {len(items)} 条；本来源已进主图 {len(bundles)} 份")

    plan, stats = build_plan(bundles, items, only)
    have = existing_edges(sorted({p["src_id"] for p in plan})) if plan else set()
    todo = [p for p in plan if (p["src_id"], p["ref"].canonical_id) not in have]
    # 按 (食材, 营养素) 去重：同一食材在抓取包里出现两次（煎饼/麦片那种）会有两份
    # bundle 指向同一个 canonical_id，不去重会让「要补 15252 条」这种数字虚高一倍，
    # 也会让后面按营养素统计的分布无法解读。
    seen: set[tuple[str, str]] = set()
    uniq: list[dict] = []
    for p in todo:
        key = (p["src_id"], p["ref"].canonical_id)
        if key in seen:
            continue
        seen.add(key)
        uniq.append(p)
    dups = len(todo) - len(uniq)
    todo = uniq

    print(
        f"可补边 {len(plan)} 条，图里已有 {len(plan) - len(todo) - dups} 条，"
        f"本次要补 {len(todo)} 条"
        + (f"（另有 {dups} 条是重复食材，已合并）" if dups else "")
    )
    print(
        f"  匹配到抓取记录 {stats.get('bundle_matched', 0)} 份"
        f"；抓取包里找不到源码 {stats.get('bundle_without_source', 0)} 份"
        f"；解析不到节点 {stats.get('unresolved', 0)} 项"
    )
    if todo:
        by_col: dict[str, int] = defaultdict(int)
        for p in todo:
            by_col[p["col_key"]] += 1
        print("  按营养素：")
        for k, v in sorted(by_col.items(), key=lambda x: -x[1]):
            print(f"    {importer.NUTRIENT_LABELS.get(k, k):<10} {v:>5} 条")

    if args.dry_run:
        print("--dry-run：没有写库。")
        return 0
    if not todo:
        print("没有要补的边。")
        return 0

    # 守门：边是靠 MATCH (a:Ingredient {canonical_id}) 建的，锚点节点不存在时
    # Cypher 只是「匹配不到」，不报错也不写 —— 脚本会打印成功、实际一条没加。
    # 这种事已经在别处发生过一次（479 个食材缺节点），这里必须先卡住。
    src_ids = sorted({p["src_id"] for p in todo})
    as_ing, _ = graph_cids(src_ids)
    absent = [c for c in src_ids if c not in as_ing]
    if absent:
        print(
            f"❌ 有 {len(absent)} 个食材在 Neo4j 里没有节点，补边会静默丢失。\n"
            f"   先跑：python -m scripts.backfill_nutrients --repair-nodes\n"
            f"   样例：{absent[:5]}"
        )
        return 3

    edges, nodes = stage(todo, args.batch)
    print(f"候选落库：{edges} 条边 / {nodes} 个营养素节点（批次 {args.batch}）")

    print("合并进主图…")
    print(f"  节点 {merge_mod.merge_nodes(args.batch)} 个")
    print(f"  边   {merge_mod.merge(args.batch)} 条")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
