"""
scripts/load_crosswalk.py —— 装载 crosswalk CSV 到 dict_alias 表

数据目录 data/crosswalk/ 下的 CSV 结构：
  ingredient_canonical.csv : canonical_id, canonical_name, category, aliases(;), nature
  nutrient_map.csv         : canonical_id, name, unit, category
  population.csv           : canonical_id, name, kind
  symptom.csv              : canonical_id, name, aliases(;), tcm_term
  taboo.csv                : canonical_id, name, taboo_type

每个实体的 canonical_name + 全部别名，统一写进 dict_alias（alias_norm 归一）。
"""
from __future__ import annotations

import csv
import sys
from pathlib import Path

import psycopg

import config
from kg.normalize import normalize_text

BASE = Path(__file__).resolve().parent.parent / "data" / "crosswalk"


def _read_csv(name: str) -> list[dict]:
    path = BASE / name
    if not path.exists():
        print(f"[skip] {name} 不存在")
        return []
    with path.open(encoding="utf-8-sig") as f:  # utf-8-sig 自动剥离 BOM
        return list(csv.DictReader(f))


def load() -> None:
    specs = [
        # (文件, entity_type, canonical_id 列, 别名列)
        ("ingredient_canonical.csv", "Ingredient", "canonical_id", "aliases"),
        ("nutrient_map.csv", "Nutrient", "canonical_id", ""),
        ("population.csv", "Population", "canonical_id", ""),
        ("symptom.csv", "Symptom", "canonical_id", "aliases"),
        ("taboo.csv", "Taboo", "canonical_id", ""),
    ]

    rows: list[tuple] = []
    for fname, entity_type, id_col, alias_col in specs:
        for row in _read_csv(fname):
            cid = row[id_col].strip()
            names = {row.get("canonical_name", "").strip(), row.get("name", "").strip()}
            names.discard("")
            if alias_col:
                for a in row.get(alias_col, "").split(";"):
                    a = a.strip()
                    if a:
                        names.add(a)
            for n in names:
                rows.append((entity_type, cid, n, normalize_text(n)))

    if not rows:
        print("没有可装载的 crosswalk 数据，请先准备 data/crosswalk/*.csv")
        sys.exit(1)

    with psycopg.connect(config.PG_DSN) as conn:
        with conn.cursor() as cur:
            for entity_type, cid, alias, alias_norm in rows:
                cur.execute(
                    "INSERT INTO dict_alias (entity_type, canonical_id, alias, alias_norm) "
                    "VALUES (%s, %s, %s, %s) "
                    "ON CONFLICT (entity_type, alias_norm, canonical_id) DO NOTHING",
                    (entity_type, cid, alias, alias_norm),
                )
    print(f"已装载 {len(rows)} 条别名记录（去重后以 DB 为准）。")


if __name__ == "__main__":
    load()
