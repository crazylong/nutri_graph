"""后台食物查询、删除、重录。检索走 AliasStore，不另写一套匹配。"""
from __future__ import annotations

import logging
import re
import unicodedata
import uuid
from typing import Any

from psycopg.types.json import Jsonb

import db
from kg import importer
from kg import subgraph as sg
from kg.normalize import get_alias_store, normalize_text

log = logging.getLogger("kg.food_admin")

PAGE_SIZE = 15
MAX_DELETE = 20
RERECORD_SOURCE = "admin_rerecord"
_SPLIT = re.compile(r"[\s,，、]+|(?:和|跟|与)")


class FoodAdminError(Exception):
    def __init__(self, code: str, message: str, status: int = 400):
        self.code = code
        self.message = message
        self.status = status
        super().__init__(message)


def _graph(cypher: str, **params) -> list[dict]:
    try:
        return sg._run(cypher, **params)
    except Exception as exc:  # noqa: BLE001
        log.warning("图谱查询失败：%s", exc)
        raise FoodAdminError("graph_unavailable", "图谱暂时连不上，稍后重试。", 503) from exc


def _names(cids: list[str]) -> dict[str, str]:
    if not cids:
        return {}
    try:
        rows = _graph(
            "UNWIND $cids AS cid "
            "OPTIONAL MATCH (n {canonical_id: cid}) "
            "RETURN cid AS cid, coalesce(n.canonical_name, n.name, '') AS name",
            cids=cids,
        )
    except FoodAdminError:
        return {}
    return {r["cid"]: r.get("name") or "" for r in rows}


def _hit(row: dict, query: str, names: dict[str, str]) -> dict:
    cid = row["canonical_id"]
    name = names.get(cid) or row.get("alias") or query
    method = "exact" if normalize_text(name) == normalize_text(query) else "alias"
    return {
        "canonical_id": cid,
        "name": name,
        "alias": row.get("alias") or "",
        "method": method,
        "entity_type": row.get("entity_type") or "",
    }


def _dedupe(rows: list[dict], query: str, names: dict[str, str]) -> list[dict]:
    out = []
    seen = set()
    for row in rows:
        cid = row.get("canonical_id")
        if not cid or cid in seen:
            continue
        seen.add(cid)
        out.append(_hit(row, query, names))
    return out


def relation_terms(raw: str) -> list[str]:
    """按空白和「和/跟/与」切开。空白要在整句去空格之前切，否则「胡萝卜 鸡蛋」会粘成一个词。"""
    text = unicodedata.normalize("NFKC", raw or "")
    out = []
    for part in _SPLIT.split(text):
        term = normalize_text(part)
        if term and term not in out:
            out.append(term)
    return out


def search(q: str) -> dict:
    """一个搜索框的分流。精确命中走库，不读进程内别名缓存。"""
    query = normalize_text(q or "")
    if not query:
        raise FoodAdminError("bad_params", "请输入要查的食物或营养素")
    store = get_alias_store()
    store.all_aliases(refresh=True)
    foods = store.exact_lookup(query, "Ingredient")
    nuts = store.exact_lookup(query, "Nutrient")
    names = _names([r["canonical_id"] for r in foods + nuts])
    food_hits = _dedupe(foods, query, names)
    nut_hits = _dedupe(nuts, query, names)
    if food_hits and nut_hits:
        return {"mode": "ambiguous", "query": query, "matches": food_hits, "nutrient_matches": nut_hits}
    if nut_hits:
        return {"mode": "nutrient", "query": query, "nutrient": nut_hits[0], "nutrient_matches": nut_hits}
    if food_hits:
        return {"mode": "food", "query": query, "matches": food_hits}
    parts = relation_terms(q or "")
    if len(parts) >= 2:
        return {"mode": "relation", "query": query, "foods": parts[:4]}
    fuzzy_food = store.fuzzy_lookup(query, "Ingredient", limit=8)
    fuzzy_nut = store.fuzzy_lookup(query, "Nutrient", limit=8)
    names = _names([r["canonical_id"] for r in fuzzy_food + fuzzy_nut])
    return {
        "mode": "fuzzy",
        "query": query,
        "matches": [_fuzzy(r, names) for r in fuzzy_food],
        "nutrient_matches": [_fuzzy(r, names) for r in fuzzy_nut],
    }


def _fuzzy(row: dict, names: dict[str, str]) -> dict:
    cid = row["canonical_id"]
    return {
        "canonical_id": cid,
        "name": names.get(cid) or row.get("alias") or "",
        "alias": row.get("alias") or "",
        "method": "fuzzy",
        "score": row.get("sim"),
        "entity_type": row.get("entity_type") or "",
    }


def nutrient_page(q: str, page: int = 1) -> dict:
    query = normalize_text(q or "")
    if page < 1:
        raise FoodAdminError("bad_params", "页码从 1 开始")
    store = get_alias_store()
    hits = _dedupe(store.exact_lookup(query, "Nutrient"), query, _names(
        [r["canonical_id"] for r in store.exact_lookup(query, "Nutrient")]
    ))
    if not hits:
        fuzzy = store.fuzzy_lookup(query, "Nutrient", limit=8)
        hits = [_fuzzy(r, _names([x["canonical_id"] for x in fuzzy])) for r in fuzzy]
    if len(hits) != 1:
        return {"mode": "choose", "query": query, "candidates": hits}
    cid = hits[0]["canonical_id"]
    skip = (page - 1) * PAGE_SIZE
    counted = _graph(
        "MATCH (:Ingredient)-[r:CONTAINS_NUTRIENT]->(n:Nutrient {canonical_id: $cid}) "
        "RETURN "
        "sum(CASE WHEN r.amount IS NOT NULL AND toFloat(r.amount) IS NOT NULL THEN 1 ELSE 0 END) AS total, "
        "sum(CASE WHEN r.amount IS NULL OR toFloat(r.amount) IS NULL THEN 1 ELSE 0 END) AS missing",
        cid=cid,
    )
    total = int((counted[0].get("total") if counted else 0) or 0)
    missing = int((counted[0].get("missing") if counted else 0) or 0)
    rows = _graph(
        "MATCH (a:Ingredient)-[r:CONTAINS_NUTRIENT]->(n:Nutrient {canonical_id: $cid}) "
        "WHERE r.amount IS NOT NULL AND toFloat(r.amount) IS NOT NULL "
        "RETURN a.canonical_id AS canonical_id, a.canonical_name AS name, "
        "       toFloat(r.amount) AS amount, "
        "       coalesce(n.unit, r.unit, '') AS unit "
        "ORDER BY amount DESC "
        "SKIP $skip LIMIT $limit",
        cid=cid, skip=skip, limit=PAGE_SIZE,
    )
    items = []
    for row in rows:
        amount = row.get("amount")
        items.append({**row, "amount": float(amount) if amount is not None else None})
    return {
        "mode": "ranked",
        "query": query,
        "nutrient": hits[0],
        "page": page,
        "page_size": PAGE_SIZE,
        "total": total,
        "missing_amount": missing,
        "items": items,
    }


def food_detail(cid: str) -> dict:
    rows = _graph(
        "MATCH (n:Ingredient {canonical_id: $cid}) "
        "OPTIONAL MATCH (n)-[r:CONTAINS_NUTRIENT]->(nut:Nutrient) "
        "RETURN n.canonical_name AS name, n.category AS category, n.nature AS nature, "
        "       n.scientific_name AS scientific_name, n.summary AS summary, "
        "       collect({id: nut.canonical_id, amount: r.amount, "
        "                unit: coalesce(nut.unit, r.unit, '')}) AS nuts",
        cid=cid,
    )
    if not rows or not rows[0].get("name"):
        raise FoodAdminError("not_found", "没有这个食材", 404)
    row = rows[0]
    aliases = _aliases(cid)
    amounts = {}
    for nut in row.get("nuts") or []:
        if not isinstance(nut, dict) or not nut.get("id") or nut.get("amount") is None:
            continue
        amounts[nut["id"]] = _num(nut["amount"])
    form = {
        "name": row["name"],
        "aliases": ";".join(a for a in aliases if normalize_text(a) != normalize_text(row["name"])),
        "category": row.get("category") or "",
        "nature": row.get("nature") or "",
        "scientific_name": row.get("scientific_name") or "",
        "english_name": "",
    }
    for col in importer.COLUMNS:
        if col.role == "nutrient":
            form[col.key] = amounts.get(col.nutrient_id, "")
    counts = _edge_counts([cid])
    try:
        graph = sg.neighborhood(cid, depth=1)
    except Exception as exc:  # noqa: BLE001
        log.warning("详情子图失败 %s：%s", cid, exc)
        raise FoodAdminError("graph_unavailable", "图谱暂时连不上，稍后重试。", 503) from exc
    return {
        "canonical_id": cid,
        "name": row["name"],
        "category": row.get("category") or "",
        "nature": row.get("nature") or "",
        "summary": row.get("summary") or "",
        "aliases": aliases,
        "edge_count": counts.get(cid, {}).get("edges", 0),
        "form": form,
        "subgraph": graph,
    }


def relation_method(term: str, canonical_name: str, raw_method: str) -> str:
    """查询词和规范名相同才是精确命中。字典里的其它叫法才标成别名。

    链接器对字典命中一律给 substring / exact，不区分「西红柿」和「番茄」。
    若直接把非 fuzzy 都标成别名，规范名也会变成「西红柿 → 西红柿（别名）」。
    """
    if raw_method == "fuzzy":
        return "fuzzy"
    if canonical_name and normalize_text(term) == normalize_text(canonical_name):
        return "exact"
    return "alias"


def relations(foods: list[str], confirm: bool = False) -> dict:
    cleaned = []
    for food in foods:
        text = (food or "").strip()
        if text and text not in cleaned:
            cleaned.append(text)
    if len(cleaned) < 2 or len(cleaned) > 4:
        raise FoodAdminError("bad_params", "需要 2 到 4 个食材")
    resolved = []
    missing = []
    for food in cleaned:
        hit = sg.resolve(food, prefer="Ingredient")
        if not hit or hit.get("label") != "Ingredient" or not hit.get("canonical_id"):
            missing.append({"term": food})
            continue
        cid = hit["canonical_id"]
        try:
            node = sg.fetch_node(cid)
        except Exception as exc:  # noqa: BLE001
            log.warning("关系图取名失败：%s", exc)
            raise FoodAdminError("graph_unavailable", "图谱暂时连不上，稍后重试。", 503) from exc
        canonical = (node or {}).get("name") or food
        resolved.append({
            "term": food,
            "canonical_id": cid,
            "name": canonical,
            "method": relation_method(food, canonical, hit.get("method") or ""),
            "aliases": [
                a for a in _aliases(cid)
                if normalize_text(a) and normalize_text(a) != normalize_text(canonical)
            ],
        })
    needs_confirm = any(item["method"] == "fuzzy" for item in resolved)
    subgraph = {"nodes": [], "edges": [], "meta": {"kind": "compare"}}
    cids = []
    for item in resolved:
        if item["canonical_id"] not in cids:
            cids.append(item["canonical_id"])
    if (not needs_confirm or confirm) and len(cids) >= 1 and len(resolved) >= 2:
        try:
            if len(cids) == 1:
                subgraph = sg.neighborhood(cids[0], depth=1)
            else:
                subgraph = sg.compare_many(cids)
        except Exception as exc:  # noqa: BLE001
            log.warning("关系图失败：%s", exc)
            raise FoodAdminError("graph_unavailable", "图谱暂时连不上，稍后重试。", 503) from exc
    return {
        "resolved": resolved,
        "missing": missing,
        "needs_confirm": needs_confirm and not confirm,
        "subgraph": subgraph,
    }


def delete_foods(cids: list[str], actor: str, preview: bool = False) -> dict:
    ids = []
    for cid in cids:
        text = (cid or "").strip()
        if text and text not in ids:
            ids.append(text)
    if not ids or len(ids) > MAX_DELETE:
        raise FoodAdminError("bad_params", f"一次删除 1 到 {MAX_DELETE} 个食材")
    counts = _edge_counts(ids)
    missing = [cid for cid in ids if not counts.get(cid, {}).get("exists")]
    if missing:
        raise FoodAdminError("not_found", "没有这些食材：" + "、".join(missing), 404)
    items = []
    for cid in ids:
        aliases = _aliases(cid)
        items.append({
            "canonical_id": cid,
            "name": counts[cid].get("name") or cid,
            "edge_count": counts[cid].get("edges", 0),
            "aliases": aliases,
        })
    if preview:
        return {"preview": True, "items": items}
    _graph(
        "UNWIND $cids AS cid "
        "MATCH (n:Ingredient {canonical_id: cid}) "
        "DETACH DELETE n",
        cids=ids,
    )
    warnings = []
    with db.connect() as conn:
        for item in items:
            conn.execute(
                "DELETE FROM dict_alias WHERE entity_type = 'Ingredient' AND canonical_id = %s",
                (item["canonical_id"],),
            )
            words = [item["name"], *item["aliases"]]
            item["tombstones"] = _tombstones(conn, words, actor)
            warning = _drop_vector(item["canonical_id"])
            if warning:
                warnings.append(warning)
                item["warning"] = warning
            conn.execute(
                "INSERT INTO kg_food_admin_action "
                "(actor, action, canonical_id, food_name, edge_count, detail) "
                "VALUES (%s, 'delete', %s, %s, %s, %s)",
                (
                    actor, item["canonical_id"], item["name"], item["edge_count"],
                    Jsonb({"aliases": item["aliases"], "warning": item.get("warning") or ""}),
                ),
            )
    get_alias_store().all_aliases(refresh=True)
    return {"preview": False, "items": items, "warnings": warnings}


def rerecord(cid: str, body: dict, actor: str) -> dict:
    detail = food_detail(cid)
    row = [body.get(col.key, "") for col in importer.COLUMNS]
    result = importer.validate_row(2, row, list(importer.COLUMNS))
    if result.verdict == "error":
        errors = [m.as_dict() for m in result.messages if m.level == "error"]
        raise FoodAdminError(
            "invalid_row",
            errors[0]["text"] if errors else "这一行校验不通过",
            422,
        )
    from kg import import_bundle

    job_id = uuid.uuid4()
    with db.connect() as conn:
        conn.execute(
            "INSERT INTO kg_import_job (job_id, filename, client_ip, template_version, "
            "  source_key, total_rows, ok_rows, warn_rows, error_rows, status, committed_at) "
            "VALUES (%s, 'admin-rerecord', 'admin', %s, %s, 1, %s, %s, 0, 'committed', now())",
            (
                job_id,
                importer.TEMPLATE_VERSION,
                RERECORD_SOURCE,
                1 if result.verdict != "warn" else 0,
                1 if result.verdict == "warn" else 0,
            ),
        )
        staged = import_bundle.stage_bundles(
            [result], job_id, "admin-rerecord", source_key=RERECORD_SOURCE, conn=conn,
        )
        if not staged:
            raise FoodAdminError("invalid_row", "没有可提交的内容", 422)
        bundle_id = staged[0]["bundle_id"]
        conn.execute(
            "UPDATE kg_food_bundle SET re_record = true, canonical_id = %s WHERE id = %s",
            (cid, bundle_id),
        )
        conn.execute(
            "INSERT INTO kg_food_admin_action "
            "(actor, action, canonical_id, food_name, edge_count, detail) "
            "VALUES (%s, 'rerecord', %s, %s, %s, %s)",
            (
                actor, cid, detail["name"], detail["edge_count"],
                Jsonb({"bundle_id": bundle_id, "submitted_name": result.name}),
            ),
        )
    return {"bundle_id": bundle_id, "canonical_id": cid, "status": "pending_review"}


def _aliases(cid: str) -> list[str]:
    with db.connect() as conn:
        rows = conn.execute(
            "SELECT alias FROM dict_alias "
            "WHERE entity_type = 'Ingredient' AND canonical_id = %s ORDER BY alias",
            (cid,),
        ).fetchall()
    return [r[0] for r in rows if r[0]]


def _edge_counts(cids: list[str]) -> dict[str, dict]:
    rows = _graph(
        "UNWIND $cids AS cid "
        "OPTIONAL MATCH (n:Ingredient {canonical_id: cid}) "
        "WITH cid, n "
        "OPTIONAL MATCH (n)-[r]-() "
        "RETURN cid AS cid, n IS NOT NULL AS exists, "
        "       coalesce(n.canonical_name, '') AS name, count(r) AS edges",
        cids=cids,
    )
    return {
        r["cid"]: {"exists": bool(r.get("exists")), "name": r.get("name") or "", "edges": int(r.get("edges") or 0)}
        for r in rows
    }


def _tombstones(conn, words: list[str], actor: str) -> list[str]:
    kept = []
    for word in words:
        key = normalize_text(word)
        if not key or key in kept:
            continue
        kept.append(key)
        conn.execute(
            "INSERT INTO kg_ingredient_request "
            "    (food_key, raw_mention, normalized, requested_by, source, status, "
            "     hit_count, reject_reason) "
            "VALUES (%s, %s, %s, %s, 'admin', 'rejected', 0, 'deleted_by_admin') "
            "ON CONFLICT (food_key) DO UPDATE SET "
            "    status = 'rejected', reject_reason = 'deleted_by_admin', "
            "    handled_by = EXCLUDED.requested_by, handled_at = now()",
            (key, word, key, actor),
        )
    return kept


def _drop_vector(cid: str) -> str:
    try:
        from rag.client import delete_entity

        delete_entity(cid)
    except Exception as exc:  # noqa: BLE001 —— 向量删不掉不回滚已经删掉的图
        log.warning("Milvus 删除失败 %s：%s", cid, exc)
        return f"向量未删除：{exc}"
    return ""


def _num(value: Any) -> str:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return ""
    if number == int(number):
        return str(int(number))
    return str(number)
