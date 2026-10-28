"""
api/food_import.py —— 公开的「Excel 导入食物」接口

挂在 /kg/import 下而不是 /admin：
  * 这个入口不登录也能用（演示场景要的就是「谁都能传一张表看效果」），
    而 /admin 整组挂了 require_admin；
  * /kg/ 前缀 nginx 已经反代到后端，新接口不用改 nginx 就能上线。

数据去哪了（2026-10-28 变更）：
  原来写独立的 demo_food，等于「导入的数据进不了问答」。
  现在统一进 kg_food_bundle，和 USDA 抓取完全同一条链路：

      POST /preview      解析 + 行级校验，落批次（不写 bundle）
      POST /{id}/commit  把可导入的行写成 pending_review 的 bundle
      （后台审核）        /console → 补录审核 → 通过 → kg.merge → 主图
      GET  /foods        看这个来源的 bundle 及其审核状态

  审核通过后，食材会出现在 /kg/subgraph、/kg/compare 和问答检索里 ——
  不是「另一套库」，只是 bundle 的第二个来源。
"""
from __future__ import annotations

import logging
import uuid
from typing import Any

import psycopg
from fastapi import APIRouter, Body, File, HTTPException, Query, Request, UploadFile
from psycopg.types.json import Jsonb

import config
from api.deps import client_ip
from kg import importer, import_bundle

log = logging.getLogger("api.food_import")

router = APIRouter(prefix="/kg/import", tags=["import"])

_TABLE_MISSING = "导入相关表不存在，请先运行 python -m scripts.migrate（012 / 013）"


# ============================================================
# 基础设施
# ============================================================
def _require_tables(conn) -> None:
    cur = conn.cursor()
    cur.execute(
        "SELECT to_regclass('public.kg_food_bundle') IS NOT NULL "
        "AND to_regclass('public.kg_import_job') IS NOT NULL "
        "AND to_regclass('public.kg_import_row') IS NOT NULL"
    )
    if not cur.fetchone()[0]:
        raise HTTPException(
            status_code=503, detail={"code": "table_missing", "message": _TABLE_MISSING}
        )


def _db():
    return psycopg.connect(config.PG_DSN)


def _enabled() -> None:
    if not config.FOOD_IMPORT_ENABLED:
        raise HTTPException(
            status_code=503,
            detail={"code": "import_disabled", "message": "导入功能暂时关闭"},
        )


def _bad(message: str, code: str = "bad_file", status: int = 400) -> HTTPException:
    return HTTPException(status_code=status, detail={"code": code, "message": message})


# ============================================================
# 元信息：把「契约」交给前端，别在前端再抄一份
# ============================================================
@router.get("/meta")
def meta():
    return {
        "enabled": config.FOOD_IMPORT_ENABLED,
        "template_version": importer.TEMPLATE_VERSION,
        "template": {
            "xlsx": f"/template/food_import_{importer.TEMPLATE_VERSION}.xlsx",
            "csv": f"/template/food_import_{importer.TEMPLATE_VERSION}.csv",
        },
        "limits": {
            "max_rows": importer.MAX_ROWS,
            "max_file_kb": importer.MAX_FILE_BYTES // 1024,
            "daily_per_ip": config.FOOD_IMPORT_DAILY_LIMIT_PER_IP,
            "preview_ttl_min": config.FOOD_IMPORT_PREVIEW_TTL_MIN,
        },
        "review": {
            "required": True,
            "console": "/console/",
            "note": "提交后进入补录审核队列，审核通过才会进图谱与问答",
        },
        "categories": list(importer.CATEGORIES),
        "natures": list(importer.NATURES),
        "columns": [
            {
                "header": c.header,
                "key": c.key,
                "role": c.role,
                "required": c.required,
                "unit": c.unit,
                "hint": c.hint,
            }
            for c in importer.COLUMNS
        ],
        "nutrients": [
            {
                "key": c.key,
                "label": importer.NUTRIENT_LABELS.get(c.key, c.key),
                "unit": c.unit,
                "required": c.required,
            }
            for c in importer.NUTRIENT_COLUMNS
        ],
    }


# ============================================================
# 解析预览
# ============================================================
def _quota_used(ip: str, sources: tuple[str, ...] | None = None) -> int:
    """这个 IP 24 小时内已经导入过几次。

    sources 传了就只算这些来源 —— 手工录入一次只有一条，和「传一个 500 行文件」
    不该共用同一个计数器，否则填 20 条就把上传额度用完了。
    """
    sql = (
        "SELECT count(*) FROM kg_import_job "
        "WHERE client_ip = %s AND created_at > now() - interval '24 hours'"
    )
    args: list[Any] = [ip]
    if sources:
        sql += " AND source_key = ANY(%s)"
        args.append(list(sources))
    with _db() as conn:
        _require_tables(conn)
        cur = conn.cursor()
        cur.execute(sql, args)
        return int(cur.fetchone()[0])


def _existing_in_graph(norms: list[str]) -> set[str]:
    """图谱里已经有这个名字（或别名）的食材。

    这张表在老库上可能还没建（迁移没跑全），查不到就当没有 ——
    这个判断只影响提示文案，不该让整个预览 500。
    """
    if not norms:
        return set()
    try:
        with _db() as conn:
            cur = conn.cursor()
            cur.execute(
                "SELECT alias_norm FROM dict_alias "
                "WHERE entity_type = 'Ingredient' AND alias_norm = ANY(%s)",
                (norms,),
            )
            return {r[0] for r in cur.fetchall()}
    except psycopg.errors.UndefinedTable:
        return set()


def _open_import_bundles(norms: list[str]) -> dict[str, str]:
    """这个来源下已经在队列里（未结）的同名 bundle → 状态。"""
    if not norms:
        return {}
    with _db() as conn:
        cur = conn.cursor()
        cur.execute(
            "SELECT food_key, status FROM kg_food_bundle "
            "WHERE source_key = %s AND food_key = ANY(%s) "
            "  AND status IN ('draft', 'pending_review', 'approved')",
            (import_bundle.IMPORT_SOURCE_KEY, norms),
        )
        return {r[0]: r[1] for r in cur.fetchall()}


@router.post("/preview")
async def preview(request: Request, file: UploadFile = File(...)):
    """上传 → 解析 → 校验 → 返回逐行结论。**不写 bundle。**"""
    _enabled()

    ip = client_ip(request) or "unknown"
    if _quota_used(ip) >= config.FOOD_IMPORT_DAILY_LIMIT_PER_IP:
        raise HTTPException(
            status_code=429,
            detail={
                "code": "quota_exhausted",
                "message": (
                    f"这个网络今天已经导入 {config.FOOD_IMPORT_DAILY_LIMIT_PER_IP} 次了，"
                    "明天再来，或者让我们在配置里调高上限"
                ),
            },
        )

    # 多读 1 字节用于判断「是否超限」，避免把超大文件整个读进内存
    blob = await file.read(importer.MAX_FILE_BYTES + 1)
    if len(blob) > importer.MAX_FILE_BYTES:
        raise _bad(
            f"文件超过 {importer.MAX_FILE_BYTES // 1024}KB，请删掉无关的列/图片后重试",
            code="file_too_large",
        )

    try:
        table = importer.parse_table(file.filename or "", blob)
    except importer.ImportError_ as exc:
        raise _bad(str(exc)) from exc

    results = importer.validate_table(table)

    # 与图谱 / 待审队列比对：只影响提示文案和「新增 or 覆盖」，不影响能不能导入
    norms = [r.payload["name_norm"] for r in results if r.payload.get("name_norm")]
    in_graph = _existing_in_graph(norms)
    in_queue = _open_import_bundles(norms)
    for r in results:
        norm = r.payload.get("name_norm")
        if not norm or r.verdict not in ("ok", "warn"):
            continue
        if norm in in_graph:
            r.verdict = "duplicate_db"
            r.messages.append(
                importer.Message(
                    "info",
                    "图谱里已有这个食材，审核通过后会挂到既有节点上并更新营养数值",
                    "名称",
                )
            )
        elif norm in in_queue:
            r.verdict = "duplicate_db"
            r.messages.append(
                importer.Message(
                    "info",
                    "这个食材已经在待审核队列里，提交会覆盖上一条",
                    "名称",
                )
            )

    summary = importer.summarize(results)
    job_id = uuid.uuid4()

    with _db() as conn:
        _require_tables(conn)
        cur = conn.cursor()
        cur.execute(
            "INSERT INTO kg_import_job (job_id, filename, client_ip, template_version, "
            "total_rows, ok_rows, warn_rows, error_rows) "
            "VALUES (%s, %s, %s, %s, %s, %s, %s, %s)",
            (
                job_id,
                (file.filename or "")[:200],
                ip,
                importer.TEMPLATE_VERSION,
                summary["total"],
                summary["ok"] + summary["duplicate_db"],
                summary["warn"],
                summary["error"] + summary["duplicate_file"],
            ),
        )
        cur.executemany(
            "INSERT INTO kg_import_row (job_id, row_no, name, verdict, messages, payload) "
            "VALUES (%s, %s, %s, %s, %s, %s)",
            [
                (
                    job_id,
                    r.row_no,
                    r.name[:80],
                    r.verdict,
                    Jsonb([m.as_dict() for m in r.messages]),
                    Jsonb(r.payload),
                )
                for r in results
            ],
        )

    log.info(
        "导入预览 job=%s ip=%s file=%s 共 %d 行，可提交 %d",
        job_id, ip, file.filename, summary["total"], summary["importable"],
    )
    return {
        "job_id": str(job_id),
        "filename": file.filename,
        "sheet": table.sheet,
        "template_version": importer.TEMPLATE_VERSION,
        "summary": summary,
        "unknown_headers": table.unknown_headers,
        "rows": [r.as_preview() for r in results],
        "quota": {
            "limit": config.FOOD_IMPORT_DAILY_LIMIT_PER_IP,
            "used": _quota_used(ip),
        },
    }


# ============================================================
# 提交审核
# ============================================================
@router.post("/{job_id}/commit")
def commit(job_id: str):
    """把批次里可导入的行写成 pending_review 的 bundle。

    同一食材重复提交会覆盖它「未结」的那份 bundle，所以这个接口天然幂等；
    已经 merged 的历史不动 —— 那是主图里已有的数据，改它要走新的审核。
    """
    _enabled()

    # 路径参数是字符串，uuid 列不接受 text 比较，先解析成 UUID
    try:
        jid = uuid.UUID(job_id)
    except (ValueError, AttributeError) as exc:
        raise _bad("批次号格式不对", code="bad_job_id", status=404) from exc

    with _db() as conn:
        _require_tables(conn)
        cur = conn.cursor()
        cur.execute(
            "SELECT status, filename, "
            "       created_at < now() - make_interval(mins => %s) AS expired "
            "FROM kg_import_job WHERE job_id = %s",
            (config.FOOD_IMPORT_PREVIEW_TTL_MIN, jid),
        )
        row = cur.fetchone()
        if row is None:
            raise _bad("没有这个导入批次，可能链接失效了", code="not_found", status=404)
        status, filename, expired = row
        if status == "committed":
            raise _bad("这一批已经提交过了", code="already_committed", status=409)
        if expired:
            raise _bad(
                f"预览超过 {config.FOOD_IMPORT_PREVIEW_TTL_MIN} 分钟已失效，请重新上传",
                code="expired", status=410,
            )

        cur.execute(
            "SELECT row_no, name, verdict, payload FROM kg_import_row "
            "WHERE job_id = %s AND verdict IN ('ok', 'warn', 'duplicate_db') "
            "ORDER BY row_no",
            (jid,),
        )
        rows = cur.fetchall()

    if not rows:
        raise _bad("这一批没有可提交的行", code="nothing_to_import", status=409)

    # 复用 importer 的 RowResult 结构，staging 只认它
    results = [
        importer.RowResult(
            row_no=row_no, name=name, verdict=verdict, messages=[], payload=payload
        )
        for row_no, name, verdict, payload in rows
    ]
    staged = import_bundle.stage_bundles(results, jid, filename or "")

    with _db() as conn:
        conn.execute(
            "UPDATE kg_import_job SET status = 'committed', committed_at = now() "
            "WHERE job_id = %s",
            (jid,),
        )

    created = sum(1 for x in staged if x["action"] == "created")
    updated = len(staged) - created
    log.info("导入提交 job=%s 新增 %d 覆盖 %d（待审核）", job_id, created, updated)
    return {
        "job_id": job_id,
        "filename": filename,
        "staged": len(staged),
        "inserted": created,
        "updated": updated,
        "skipped": 0,
        "items": staged,
        "review": {
            "required": True,
            "console": "/console/",
            "note": "已进入补录审核队列，审核通过后可被问答与图谱检索",
        },
    }


# ============================================================
# 手工录入：一次一条
# ============================================================
@router.post("/manual")
def create_manual(request: Request, body: dict[str, Any] = Body(...)):
    """填一条食物 → 校验 → 进审核队列。

    为什么不做成「另一套校验」：手工录入和上传表格的差别只在传输方式，
    数据契约、单位、上限、分类枚举完全一样。所以这里把表单字段摊成
    一行「表格」，交给同一个 importer.validate_row —— 规则只有一份，
    页面上填不进去的值，接口也进不去。
    """
    _enabled()

    ip = client_ip(request) or "unknown"
    if _quota_used(ip, (import_bundle.MANUAL_SOURCE_KEY,)) >= config.FOOD_MANUAL_DAILY_LIMIT_PER_IP:
        raise HTTPException(
            status_code=429,
            detail={
                "code": "quota_exhausted",
                "message": (
                    f"这个网络今天已经录入 {config.FOOD_MANUAL_DAILY_LIMIT_PER_IP} 条了，"
                    "明天再来，或者让我们在配置里调高上限"
                ),
            },
        )

    # 只认模板里的列；多传的键忽略（前端多带一个 csrf 之类不该报 500）
    row = [body.get(c.key, "") for c in importer.COLUMNS]
    r = importer.validate_row(2, row, list(importer.COLUMNS))

    if r.verdict == "error":
        errors = [m.as_dict() for m in r.messages if m.level == "error"]
        raise HTTPException(
            status_code=422,
            detail={
                "code": "invalid_row",
                "message": errors[0]["text"] if errors else "这一行校验不通过",
                "errors": errors,
                "warnings": [m.as_dict() for m in r.messages if m.level == "warn"],
            },
        )

    job_id = uuid.uuid4()
    with _db() as conn:
        _require_tables(conn)
        conn.execute(
            "INSERT INTO kg_import_job (job_id, filename, client_ip, template_version, "
            "  source_key, total_rows, ok_rows, warn_rows, error_rows, status, committed_at) "
            "VALUES (%s,%s,%s,%s,%s,1,%s,%s,0,'committed',now())",
            (
                job_id,
                "manual-entry",
                ip,
                importer.TEMPLATE_VERSION,
                import_bundle.MANUAL_SOURCE_KEY,
                1 if r.verdict != "warn" else 0,
                1 if r.verdict == "warn" else 0,
            ),
        )
        conn.execute(
            "INSERT INTO kg_import_row (job_id, row_no, name, verdict, messages, payload) "
            "VALUES (%s,2,%s,%s,%s,%s)",
            (
                job_id,
                r.name[:80],
                r.verdict,
                Jsonb([m.as_dict() for m in r.messages]),
                Jsonb(r.payload),
            ),
        )

    staged = import_bundle.stage_bundles(
        [r], job_id, "manual-entry", source_key=import_bundle.MANUAL_SOURCE_KEY
    )
    item = staged[0] if staged else {}
    log.info(
        "手工录入 job=%s ip=%s %s（%s）",
        job_id, ip, r.name, item.get("action", "?"),
    )
    return {
        "job_id": str(job_id),
        "bundle_id": item.get("bundle_id"),
        "name": r.name,
        "action": item.get("action", ""),
        "verdict": r.verdict,
        "warnings": [m.as_dict() for m in r.messages if m.level == "warn"],
        "review": {
            "required": True,
            "console": "/console/",
            "note": "已进入补录审核队列，审核通过后可被问答与图谱检索",
        },
    }



# ============================================================
# 看板：这次导入进来的食物 + 审核状态
# ============================================================
@router.get("/foods")
def list_foods(
    q: str = Query("", max_length=40),
    category: str = Query("", max_length=20),
    status: str = Query("", max_length=20),
    limit: int = Query(500, ge=1, le=500),
    offset: int = Query(0, ge=0),
):
    """导入来源的 bundle。q 同时匹配名称、归一名与别名。"""
    with _db() as conn:
        _require_tables(conn)
    return import_bundle.list_import_bundles(
        q=q, category=category, status=status, limit=limit, offset=offset
    )


@router.get("/jobs")
def list_jobs(limit: int = Query(10, ge=1, le=50)):
    with _db() as conn:
        _require_tables(conn)
        cur = conn.cursor()
        cur.execute(
            "SELECT job_id, filename, source_key, total_rows, ok_rows, warn_rows, "
            "error_rows, status, created_at, committed_at FROM kg_import_job "
            "ORDER BY created_at DESC LIMIT %s",
            (limit,),
        )
        cols = [d[0] for d in cur.description]
        items = []
        for r in cur.fetchall():
            d = dict(zip(cols, r))
            d["job_id"] = str(d["job_id"])
            for k in ("created_at", "committed_at"):
                if d.get(k) is not None:
                    d[k] = d[k].isoformat(timespec="seconds")
            items.append(d)
    return {"count": len(items), "items": items}


@router.get("/stats")
def stats() -> dict[str, Any]:
    """审核队列的当前分布，页面顶部提示用。"""
    with _db() as conn:
        _require_tables(conn)
        cur = conn.cursor()
        cur.execute(
            "SELECT status, count(*) FROM kg_food_bundle "
            "WHERE source_key = ANY(%s) GROUP BY status",
            (list(import_bundle.IMPORT_SOURCE_KEYS),),
        )
        by_status = {r[0]: r[1] for r in cur.fetchall()}
    return {
        "by_status": by_status,
        "pending": by_status.get("pending_review", 0),
        "merged": by_status.get("merged", 0),
        "console": "/console/",
    }
