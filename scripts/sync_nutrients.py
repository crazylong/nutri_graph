"""
scripts/sync_nutrients.py —— 把 nutrient_map.csv 的营养素节点与别名同步进库

为什么单独一个脚本，而不是跑 scripts/load_seed：
  load_seed 会把食材、人群、症状、禁忌、种子边全部重装一遍。线上只是「新增几个
  营养素节点」，不该让一次加节点有机会顺带改动那批已经审过的数据。
  这里只碰 Nutrient 这一类，幂等，可以随时重跑。

一件事要同时改三处，图谱才算「认识这个营养素」：

  1. Neo4j 里的 `:Nutrient` 节点（name / unit / category）—— 边要挂得上；
  2. dict_alias（中文名 + 别名，如「硫胺素」→ 维生素B1）—— 用户换个说法也要能链到；
  3. `kg/nutrients.py` 的固定 NUT id（**代码**）—— 审核时把 (中文名,单位) 翻成 id。

本脚本负责 1 和 2。第 3 处是代码，改完必须重启服务才生效；
`tests/test_importer.py::test_nutrient_map_csv_matches_resolver` 守的就是
「CSV 加了、代码忘了加」这种漂移 —— 那会导致节点建出来了，但导入时被静默跳过。

用法：
  python -m scripts.sync_nutrients            # 同步
  python -m scripts.sync_nutrients --dry-run  # 只看会改什么
"""
from __future__ import annotations

import argparse
import csv
import logging
from pathlib import Path

import psycopg
from neo4j import GraphDatabase

import config
from kg.normalize import normalize_text

log = logging.getLogger("scripts.sync_nutrients")

CROSSWALK = Path(__file__).resolve().parent.parent / "data" / "crosswalk"
CSV_PATH = CROSSWALK / "nutrient_map.csv"


def read_nutrients() -> list[dict]:
    with CSV_PATH.open(encoding="utf-8-sig") as f:
        rows = [r for r in csv.DictReader(f) if (r.get("canonical_id") or "").strip()]
    out = []
    for r in rows:
        out.append(
            {
                "canonical_id": r["canonical_id"].strip(),
                "name": (r.get("name") or "").strip(),
                "unit": (r.get("unit") or "").strip(),
                "category": (r.get("category") or "").strip(),
                "aliases": [a.strip() for a in (r.get("aliases") or "").split(";") if a.strip()],
            }
        )
    return out


def _existing_nodes(driver, ids: list[str]) -> set[str]:
    with driver.session() as s:
        rows = s.run(
            "MATCH (n:Nutrient) WHERE n.canonical_id IN $ids "
            "RETURN n.canonical_id AS cid",
            ids=ids,
        )
        return {r["cid"] for r in rows}


def upsert_nodes(driver, rows: list[dict]) -> tuple[int, int]:
    """MERGE 节点。只有新节点计入 created，已有节点只补 name/unit/category。"""
    have = _existing_nodes(driver, [r["canonical_id"] for r in rows])
    payload = [
        {
            "canonical_id": r["canonical_id"],
            "name": r["name"],
            "unit": r["unit"],
            "category": r["category"],
        }
        for r in rows
    ]
    with driver.session() as s:
        s.run(
            "UNWIND $rows AS row "
            "MERGE (n:Nutrient {canonical_id: row.canonical_id}) "
            # 与 scripts/load_seed.py 的属性映射保持一致，否则两条装载路径会写出两种形状
            "SET n.name = row.name, n.unit = row.unit, n.category = row.category",
            rows=payload,
        )
    return len([r for r in rows if r["canonical_id"] not in have]), len(have)


def upsert_aliases(rows: list[dict]) -> int:
    """写 dict_alias（name + aliases）。

    与 kg.promote.sync_aliases 的差异：营养素是**共享词汇**，不像食材那样
    「别名被占用就不能抢」—— 同一个「硒」只该有一个节点，重跑时只需要跳过
    已经指向自己的那条。这里用 ON CONFLICT DO NOTHING 让唯一约束兜住。
    """
    written = 0
    with psycopg.connect(config.PG_DSN) as conn:
        for r in rows:
            names = list(dict.fromkeys([r["name"], *r["aliases"]]))
            for n in names:
                norm = normalize_text(n)
                if not norm:
                    continue
                cur = conn.execute(
                    "INSERT INTO dict_alias (entity_type, canonical_id, alias, alias_norm) "
                    "VALUES ('Nutrient', %s, %s, %s) "
                    "ON CONFLICT (entity_type, alias_norm, canonical_id) DO NOTHING",
                    (r["canonical_id"], n, norm),
                )
                written += cur.rowcount
    return written


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="同步营养素节点与别名")
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args(argv)

    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s %(message)s")

    rows = read_nutrients()
    print(f"crosswalk 里有 {len(rows)} 个营养素")

    if args.dry_run:
        driver = GraphDatabase.driver(
            config.NEO4J_URI, auth=(config.NEO4J_USER, config.NEO4J_PASSWORD)
        )
        try:
            have = _existing_nodes(driver, [r["canonical_id"] for r in rows])
        finally:
            driver.close()
        missing = [r for r in rows if r["canonical_id"] not in have]
        print(f"图里已有 {len(have)} 个，缺 {len(missing)} 个：")
        for r in missing:
            print(f"  {r['canonical_id']}  {r['name']} ({r['unit']})")
        print("--dry-run：没有写库。")
        return 0

    driver = GraphDatabase.driver(
        config.NEO4J_URI, auth=(config.NEO4J_USER, config.NEO4J_PASSWORD)
    )
    try:
        created, updated = upsert_nodes(driver, rows)
    finally:
        driver.close()

    aliases = upsert_aliases(rows)
    print(f"节点：新建 {created} 个 / 更新 {updated} 个")
    print(f"别名：新增 {aliases} 条（已存在的跳过）")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
