"""未收录词的反查。种子同义在字级模糊之前，避免「牛油果」被「牛油」截住。"""
from __future__ import annotations

import csv
import logging
from pathlib import Path

from neo4j import GraphDatabase

import config
from kg.normalize import AliasStore, normalize_text
from kg.request import _NON_FOOD_TERMS
from kg.synonym import suggest

log = logging.getLogger("kg.lookup")

_SEED = Path(__file__).resolve().parents[1] / "data" / "synonyms" / "ingredient_synonyms.csv"
_synonyms: list[dict] | None = None


def load_synonyms(refresh: bool = False) -> list[dict]:
    global _synonyms
    if _synonyms is not None and not refresh:
        return _synonyms
    rows: list[dict] = []
    if _SEED.exists():
        with _SEED.open(encoding="utf-8-sig") as f:
            for row in csv.DictReader(f):
                alias = normalize_text(row.get("alias") or "")
                name = normalize_text(row.get("canonical_name") or "")
                if alias and name:
                    rows.append({
                        "alias": alias,
                        "canonical_name": name,
                        "note": (row.get("note") or "").strip(),
                    })
    _synonyms = rows
    return _synonyms


def graph_by_name(name: str) -> list[dict]:
    """按规范名精确点名。图不可用时返回空，不让整次反查失败。"""
    if not name:
        return []
    driver = None
    try:
        driver = GraphDatabase.driver(
            config.NEO4J_URI, auth=(config.NEO4J_USER, config.NEO4J_PASSWORD)
        )
        with driver.session() as session:
            records = session.run(
                "MATCH (n:Ingredient) WHERE n.canonical_name = $name "
                "RETURN n.canonical_id AS cid, n.canonical_name AS name LIMIT 5",
                name=name,
            )
            return [
                {"canonical_id": rec["cid"], "name": rec["name"]}
                for rec in records
                if rec["cid"]
            ]
    except Exception:  # noqa: BLE001 —— 图挂了仍要能看别名和种子
        log.warning("图谱点名失败：%s", name, exc_info=True)
        return []
    finally:
        if driver is not None:
            driver.close()


def _hit(layer: str, canonical_id: str, name: str, score, why: str) -> dict:
    return {
        "layer": layer,
        "canonical_id": canonical_id or "",
        "name": name,
        "score": score,
        "why": why,
    }


def _ids_for_name(store: AliasStore, name: str) -> list[dict]:
    """规范名 → id。先别名表，再图谱点名。"""
    found = []
    for row in store.exact_lookup(name, entity_type="Ingredient"):
        found.append({
            "canonical_id": row["canonical_id"],
            "name": row.get("alias") or name,
        })
    if found:
        return found
    return graph_by_name(name)


def match(term: str) -> dict:
    """L1 精确别名 → L2 规范名相同 → 同义种子 → L3 字级模糊。种子不被模糊截断。"""
    term = normalize_text(term)
    if not term:
        return {"term": "", "hits": [], "verdict": "not_found"}
    if term in _NON_FOOD_TERMS:
        return {"term": term, "hits": [], "verdict": "not_food"}

    store = AliasStore(config.PG_DSN)
    exact = [
        row for row in store.exact_lookup(term)
        if row["entity_type"] == "Ingredient"
    ]
    if exact:
        return {
            "term": term,
            "hits": [
                _hit("alias", row["canonical_id"], row.get("alias") or term, 1.0, "别名表精确命中")
                for row in exact
            ],
            "verdict": "exists",
        }

    named = graph_by_name(term)
    if named:
        return {
            "term": term,
            "hits": [
                _hit("graph_name", row["canonical_id"], row["name"], None, "图谱规范名相同")
                for row in named
            ],
            "verdict": "exists",
        }

    hits: list[dict] = []
    for row in load_synonyms():
        if row["alias"] != term:
            continue
        resolved = _ids_for_name(store, row["canonical_name"])
        if not resolved:
            hits.append(_hit(
                "synonym", "", row["canonical_name"], None,
                "同义种子里有这个规范名，图谱和别名表都没有",
            ))
            continue
        for item in resolved:
            hits.append(_hit(
                "synonym", item["canonical_id"], row["canonical_name"], None,
                row["note"] or "同义种子",
            ))

    for row in store.fuzzy_lookup(
        term, entity_type="Ingredient", min_sim=config.FOOD_NEAR_MISS_MIN_SIM,
    ):
        hits.append(_hit(
            "fuzzy", row["canonical_id"], row.get("alias") or "", row.get("sim"), "字级相似",
        ))

    if not hits and config.TRIAGE_SYNONYM_ENABLED:
        suggested = suggest(term)
        if suggested and suggested.get("canonical_name"):
            hits.append(_hit(
                "model", "", suggested["canonical_name"], suggested.get("confidence"),
                "模型建议，需人工确认",
            ))

    return {
        "term": term,
        "hits": hits,
        "verdict": "likely_exists" if hits else "not_found",
    }
