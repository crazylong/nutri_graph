"""
scripts/load_seed.py —— 把演示数据（crosswalk 节点 + 种子边）写入 Neo4j

用法：
  python -m scripts.load_seed          # 装载全部
  python -m scripts.load_seed --edges-only   # 只装关系（节点已存在时）

幂等：节点按 canonical_id MERGE；关系按 relation_key 确定性 MERGE。
"""
from __future__ import annotations

import argparse
import csv
import logging
from pathlib import Path

from neo4j import GraphDatabase

import config
from kg.schema import relation_key, sources_to_json

log = logging.getLogger("load_seed")

BASE = Path(__file__).resolve().parent.parent
CROSSWALK = BASE / "data" / "crosswalk"
SEED_EDGES = BASE / "data" / "seeds" / "seed_edges.csv"


def _read_csv(path: Path) -> list[dict]:
    if not path.exists():
        log.warning("文件不存在：%s", path)
        return []
    with path.open(encoding="utf-8-sig") as f:  # utf-8-sig 自动剥离 BOM
        return list(csv.DictReader(f))


def _split_aliases(row: dict, key: str = "aliases") -> list[str]:
    return [a.strip() for a in row.get(key, "").split(";") if a.strip()]


def merge_ingredients(driver) -> None:
    rows = _read_csv(CROSSWALK / "ingredient_canonical.csv")
    q = """
    UNWIND $rows AS row
    MERGE (n:Ingredient {canonical_id: row.canonical_id})
    SET n.canonical_name = row.canonical_name,
        n.category = row.category,
        n.nature = row.nature,
        n.aliases = row.aliases,
        n.summary = coalesce(n.summary, row.canonical_name)
    """
    data = [
        {
            "canonical_id": r["canonical_id"],
            "canonical_name": r["canonical_name"],
            "category": r["category"],
            "nature": r.get("nature", ""),
            "aliases": _split_aliases(r),
        }
        for r in rows
    ]
    with driver.session() as s:
        s.run(q, rows=data)
    log.info("Ingredient 节点：%d", len(data))


def _merge_generic(driver, label: str, path: Path, prop_map: dict) -> None:
    rows = _read_csv(path)
    q = f"""
    UNWIND $rows AS row
    MERGE (n:{label} {{canonical_id: row.canonical_id}})
    SET n += row.props
    """
    data = [
        {
            "canonical_id": r["canonical_id"],
            "props": {k: r.get(c, "") for k, c in prop_map.items()},
        }
        for r in rows
    ]
    with driver.session() as s:
        s.run(q, rows=data)
    log.info("%s 节点：%d", label, len(data))


def _read_edges(path: Path) -> list[dict]:
    """读取种子边 CSV（跳过 # 注释行和空行，第一行非注释行为表头）。"""
    if not path.exists():
        log.warning("文件不存在：%s", path)
        return []
    lines = []
    with path.open(encoding="utf-8-sig") as f:
        for line in f:
            line = line.strip()
            if not line or line.startswith("#"):
                continue
            lines.append(line)
    if not lines:
        return []
    header = lines[0].split(",")
    out = []
    for line in lines[1:]:
        parts = line.split(",")
        out.append(dict(zip(header, parts)))
    return out


def merge_edges(driver) -> None:
    rows = _read_edges(SEED_EDGES)

    REL_META = {
        "CONTAINS_NUTRIENT": ("Ingredient", "Nutrient"),
        "RECOMMEND_PAIR": ("Ingredient", "Ingredient"),
        "INCOMPATIBLE_WITH": ("Ingredient", "Ingredient"),
        "CONTRA_POPULATION": ("Ingredient", "Population"),
        "SUBSTITUTE_FOR": ("Ingredient", "Ingredient"),
        "CONTRA_ITEM": ("Ingredient", "Taboo"),
        "HAS_SYMPTOM": ("Population", "Symptom"),
    }

    data = []
    for r in rows:
        rel = r["rel"]
        if rel not in REL_META:
            log.warning("未知关系类型，跳过：%s", rel)
            continue
        src_label, dst_label = REL_META[rel]
        data.append(
            {
                "src_cid": r["src_cid"],
                "dst_cid": r["dst_cid"],
                "src_label": src_label,
                "dst_label": dst_label,
                "rel": rel,
                "relation_key": relation_key(r["src_cid"], rel, r["dst_cid"]),
                "evidence_type": r["evidence_type"],
                "evidence_level": r["evidence_level"],
                "claim_text": r["claim_text"],
                "sources": sources_to_json(
                    [
                        {
                            "type": "document",
                            "title": r["source_title"],
                            "publisher": r["source_publisher"],
                            "year": r["source_year"],
                        }
                    ]
                ),
            }
        )

    # 关系 label 是动态的（用户输入之外，来自受控枚举），逐条确定性 MERGE。
    # 数量少（演示数据），性能可接受；生产批量写入走 kg/merge.py 的 UNWIND。
    for d in data:
        rel = d["rel"]
        cypher = (
            f"MATCH (a {{canonical_id: $src_cid}}) "
            f"MATCH (b {{canonical_id: $dst_cid}}) "
            f"MERGE (a)-[r:{rel} {{relation_key: $relation_key}}]->(b) "
            f"SET r.evidence_type = $evidence_type, "
            f"r.evidence_level = $evidence_level, "
            f"r.claim_text = $claim_text, "
            f"r.sources = $sources, "
            f"r.review_status = 'approved'"
        )
        with driver.session() as s:
            s.run(cypher, **d)

    log.info("关系边：%d", len(data))


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--edges-only", action="store_true", help="只装关系，跳过节点")
    args = parser.parse_args()

    driver = GraphDatabase.driver(
        config.NEO4J_URI, auth=(config.NEO4J_USER, config.NEO4J_PASSWORD)
    )
    try:
        if not args.edges_only:
            merge_ingredients(driver)
            _merge_generic(driver, "Nutrient", CROSSWALK / "nutrient_map.csv",
                           {"name": "name", "unit": "unit", "category": "category"})
            _merge_generic(driver, "Population", CROSSWALK / "population.csv",
                           {"name": "name", "kind": "kind"})
            _merge_generic(driver, "Symptom", CROSSWALK / "symptom.csv",
                           {"name": "name", "tcm_term": "tcm_term"})
            _merge_generic(driver, "Taboo", CROSSWALK / "taboo.csv",
                           {"name": "name", "taboo_type": "taboo_type"})
        merge_edges(driver)
    finally:
        driver.close()

    print("演示数据装载完成（可重复运行，幂等）。")


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    main()
