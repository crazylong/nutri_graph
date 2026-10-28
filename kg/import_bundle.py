"""
kg/import_bundle.py —— Excel 导入的产物落到审核队列

为什么不再单开一张 demo_food：

  导入的数据和抓取的数据是同一类东西 —— 都是「某个食材每 100g 含什么」，
  只是一个来自 USDA API，一个来自用户上传的表。原来写入独立表等于承认
  「导入的数据进不了问答」，那它就只能当摆设。

  现在统一成 kg_food_bundle：

      上传表格 → bundle(pending_review) → 后台审核 → kg_node_candidate /
      kg_candidate → kg.merge → Neo4j → 问答和图谱探索都查得到

  这样「导入」不是另一套系统，只是 bundle 的第二个来源。

和抓取侧（kg/fetch.py::_persist）的两处差异，都是刻意的：

  1. **不新建 kg_ingredient_request**。那张表是「用户问过但库里没有」的登记，
     导入不是用户提问，硬塞进去会污染「未收录 → 解决率」这个指标。
     bundle.request_id 留空即可（该列可空）。
  2. **不跑 kg.verify 评分**。评分是给「自动匹配结果」用的：USDA 搜「黑豆」
     可能返回黑豆粉、黑豆罐头。导入是用户明确指定每一行的，没有匹配误差，
     给个固定分（IMPORT_SCORE）就行；它永远低于自动通过门槛，必须人工看。
"""
from __future__ import annotations

import logging
from typing import Any

import psycopg
from psycopg.types.json import Jsonb

import config
from kg import importer

log = logging.getLogger("kg.import_bundle")

# 导入来源的固定标识。抓取是 usda_fdc，将来接中国食物成分表离线包可以另起一个，
# 只要不叫 usda_fdc，就不会和抓取侧「先删后插」的覆盖逻辑互相干扰。
#
# 现在有三个「导入」来源，区别只在数据从哪来、谁负责，链路是同一条：
#   excel_import —— 网友在 /import.html 上传的表（人工填的，必须人审）
#   manual_entry —— 网友在 /import.html 一条一条填的（同上，只是没走文件）
#   fcd_crawler  —— 中国食物成分表离线抓取包（机构测定值，脚本批量入队）
# 三者都写 kg_food_bundle → 审核 → kg.merge，页面和问答看到的是同一份数据。
EXCEL_SOURCE_KEY = "excel_import"
MANUAL_SOURCE_KEY = "manual_entry"
FCD_SOURCE_KEY = "fcd_crawler"

# 兼容旧调用点（默认来源仍是 Excel）
IMPORT_SOURCE_KEY = EXCEL_SOURCE_KEY

# 页面上「导入进来的食物」= 这几个来源的并集
IMPORT_SOURCE_KEYS: tuple[str, ...] = (
    EXCEL_SOURCE_KEY,
    MANUAL_SOURCE_KEY,
    FCD_SOURCE_KEY,
)

# 固定分。低于 FOOD_AUTO_APPROVE_MIN_SCORE(0.9) → 永远进人工审核，
# 这是有意的：表是用户上传的，没人看过就进主图，等于把审核环节取消了。
IMPORT_SCORE = 0.8

# 可以进审核队列的判定（error / duplicate_file 已在 validate 阶段挡掉）
_PASS_VERDICTS = ("ok", "warn", "duplicate_db")

_LABEL_TO_KEY = {v: k for k, v in importer.NUTRIENT_LABELS.items()}

_OPEN_STATUS = ("draft", "pending_review", "approved")


class ImportBundleError(RuntimeError):
    """导入落库失败（批次号不对、没有可入库的行等）。"""


def _sources(payload: dict, job_id: str, filename: str, row_no: int) -> list[dict]:
    return [
        {
            "type": "file",
            "title": payload.get("source") or "",
            "publisher": "",
            "external_id": filename,
            "locator": f"job:{job_id} 第{row_no}行",
        }
    ]


def _items(payload: dict, job_id: str, row_no: int) -> list[tuple]:
    """bundle 的逐项产物。字段名要和 kg.promote.approve_bundle 读的键一致。"""
    locator = f"import:{job_id}#r{row_no}"
    out: list[tuple] = []
    for name in (
        "canonical_name",
        "english_name",
        "scientific_name",
        "category",
        "nature",
        "source_desc",
        "note",
    ):
        value = payload.get("name") if name == "canonical_name" else payload.get(name, "")
        out.append(("field", name, str(value or ""), "", locator, bool(value)))
    for nut in (payload.get("nutrients") or {}).values():
        out.append(
            ("nutrient", nut["label"], str(nut["v"]), nut["unit"], locator, True)
        )
    return out


def _stage_one(
    conn, payload: dict, job_id, filename: str, row_no: int, verdict: str,
    source_key: str = IMPORT_SOURCE_KEY,
) -> dict:
    food_key = payload["name_norm"]
    name = payload["name"]

    # 同一食材的并发上传要串行化：两个人同时传同一份表，不加锁会各自 SELECT 到空，
    # 然后一起 INSERT，撞上 uq_open_import_bundle。锁在事务结束时自动释放，
    # 比「撞了回滚再重试」安全 —— 重试会把同批次前面已写入的行一起回滚掉。
    conn.execute("SELECT pg_advisory_xact_lock(hashtext(%s))", (f"{source_key}:{food_key}",))

    row = conn.execute(
        "SELECT id FROM kg_food_bundle "
        "WHERE food_key = %s AND source_key = %s "
        "  AND status = ANY(%s) ORDER BY id LIMIT 1 FOR UPDATE",
        (food_key, source_key, list(_OPEN_STATUS)),
    ).fetchone()

    aliases = list(payload.get("aliases") or [])
    sources = Jsonb(_sources(payload, job_id, filename, row_no))
    detail = Jsonb(
        {
            "origin": source_key,
            "job_id": str(job_id),
            "row_no": row_no,
            "verdict": verdict,
        }
    )

    if row:
        bundle_id, action = row[0], "updated"
        conn.execute(
            "UPDATE kg_food_bundle SET "
            "  canonical_name = %s, english_name = %s, scientific_name = %s, "
            "  category = %s, nature = %s, aliases = %s, "
            "  external_id = %s, source_desc = %s, "
            "  score_total = %s, score_detail = %s, source_count = 1, sources = %s, "
            "  hard_veto = '', status = 'pending_review', evidence_type = 'measured', "
            "  reviewer = '', reviewed_at = NULL, reject_reason = '' "
            "WHERE id = %s",
            (
                name,
                payload.get("english_name") or "",
                payload.get("scientific_name") or "",
                payload.get("category") or "其他",
                payload.get("nature") or "",
                aliases,
                f"{job_id}#{row_no}",
                payload.get("source") or "",
                IMPORT_SCORE,
                detail,
                sources,
                bundle_id,
            ),
        )
        conn.execute("DELETE FROM kg_food_bundle_item WHERE bundle_id = %s", (bundle_id,))
    else:
        action = "created"
        bundle_id = conn.execute(
            "INSERT INTO kg_food_bundle "
            "  (request_id, food_key, canonical_name, english_name, scientific_name, "
            "   category, nature, aliases, source_key, external_id, source_desc, "
            "   score_total, score_detail, source_count, sources, hard_veto, status, "
            "   evidence_type) "
            "VALUES (NULL,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,1,%s,'','pending_review',"
            "        'measured') "
            "RETURNING id",
            (
                food_key,
                name,
                payload.get("english_name") or "",
                payload.get("scientific_name") or "",
                payload.get("category") or "其他",
                payload.get("nature") or "",
                aliases,
                source_key,
                f"{job_id}#{row_no}",
                payload.get("source") or "",
                IMPORT_SCORE,
                detail,
                sources,
            ),
        ).fetchone()[0]

    items = _items(payload, job_id, row_no)
    if items:
        with conn.cursor() as cur:
            cur.executemany(
                "INSERT INTO kg_food_bundle_item "
                "(bundle_id, item_type, field_name, field_value, unit, source_locator, "
                " grounded, item_status) VALUES (%s,%s,%s,%s,%s,%s,%s,'pending')",
                [(bundle_id, *it) for it in items],
            )

    return {"bundle_id": bundle_id, "name": name, "row_no": row_no, "action": action}


def stage_bundles(
    rows,
    job_id,
    filename: str = "",
    source_key: str = IMPORT_SOURCE_KEY,
) -> list[dict]:
    """把校验通过的行写进审核队列。整批一个事务：要么全进，要么全不进。

    重复上传同一食材会覆盖它「未结」的那份 bundle（pending_review / approved），
    已经 merged 的历史不动 —— 那是主图里已有的数据，改它要走新的审核。
    """
    todo = [r for r in rows if r.verdict in _PASS_VERDICTS and r.payload.get("name_norm")]
    if not todo:
        return []

    out: list[dict] = []
    with psycopg.connect(config.PG_DSN) as conn:
        for r in todo:
            out.append(
                _stage_one(conn, r.payload, job_id, filename, r.row_no, r.verdict, source_key)
            )
    log.info(
        "导入落库 source=%s job=%s 共 %d 行：新增 %d 覆盖 %d",
        source_key, job_id, len(out),
        sum(1 for x in out if x["action"] == "created"),
        sum(1 for x in out if x["action"] == "updated"),
    )
    return out


# ============================================================
# 看板：导入进来的 bundle（带审核状态）
# ============================================================
def list_import_bundles(
    q: str = "",
    category: str = "",
    status: str = "",
    limit: int = 500,
    offset: int = 0,
    sources: tuple[str, ...] = IMPORT_SOURCE_KEYS,
) -> dict[str, Any]:
    where = ["b.source_key = ANY(%s)"]
    args: list[Any] = [list(sources)]
    if q.strip():
        pat = f"%{q.strip()}%"
        where.append("(b.canonical_name ILIKE %s OR b.food_key ILIKE %s "
                     "OR EXISTS (SELECT 1 FROM unnest(b.aliases) a WHERE a ILIKE %s))")
        args.extend([pat, pat, pat])
    if category.strip():
        where.append("b.category = %s")
        args.append(category.strip())
    if status.strip():
        where.append("b.status = %s")
        args.append(status.strip())
    clause = " AND ".join(where)

    with psycopg.connect(config.PG_DSN) as conn:
        cur = conn.cursor()
        cur.execute(f"SELECT count(*) FROM kg_food_bundle b WHERE {clause}", args)
        total = int(cur.fetchone()[0])

        cur.execute(
            f"SELECT b.id, b.canonical_name, b.category, b.nature, b.english_name, "
            f"       b.scientific_name, b.aliases, b.source_desc, b.status, "
            f"       b.canonical_id, b.created_at, b.reviewed_at "
            f"FROM kg_food_bundle b WHERE {clause} "
            f"ORDER BY b.created_at DESC, b.id DESC LIMIT %s OFFSET %s",
            [*args, limit, offset],
        )
        cols = [d[0] for d in cur.description]
        rows = [dict(zip(cols, r)) for r in cur.fetchall()]
        ids = [r["id"] for r in rows]

        items: dict[int, dict] = {i: {} for i in ids}
        if ids:
            cur.execute(
                "SELECT bundle_id, field_name, field_value, unit FROM kg_food_bundle_item "
                "WHERE bundle_id = ANY(%s) AND item_type = 'nutrient' ORDER BY id",
                (ids,),
            )
            for bundle_id, label, value, unit in cur.fetchall():
                key = _LABEL_TO_KEY.get(label)
                if not key:
                    continue
                try:
                    v = float(value)
                except (TypeError, ValueError):
                    continue
                items[bundle_id][key] = {"v": v, "unit": unit, "label": label}

        cur.execute(
            f"SELECT b.category, count(*) FROM kg_food_bundle b "
            f"WHERE b.source_key = ANY(%s) GROUP BY b.category ORDER BY count(*) DESC",
            (list(sources),),
        )
        by_cat = [{"category": c, "count": n} for c, n in cur.fetchall()]

    out = []
    for r in rows:
        created = r["created_at"]
        reviewed = r["reviewed_at"]
        out.append(
            {
                "id": r["id"],
                "bundle_id": r["id"],
                "name": r["canonical_name"],
                "aliases": list(r["aliases"] or []),
                "category": r["category"],
                "nature": r["nature"],
                "english_name": r["english_name"],
                "scientific_name": r["scientific_name"],
                "source": r["source_desc"],
                "status": r["status"],
                "canonical_id": r["canonical_id"],
                "nutrients": items.get(r["id"], {}),
                "created_at": created.isoformat(timespec="seconds") if created else "",
                "reviewed_at": reviewed.isoformat(timespec="seconds") if reviewed else "",
            }
        )

    return {
        "count": total,
        "limit": limit,
        "offset": offset,
        "items": out,
        "by_category": by_cat,
    }
