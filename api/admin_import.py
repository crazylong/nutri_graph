"""
api/admin_import.py —— 管理员批量导入通道（挂 /admin/imports）

和公开导入（api/food_import.py，挂在 /kg/import）的区别，只在「谁在用、怎么计额」：

  * 公开页 /import.html 不登录、按 **IP** 计额（防止刷接口）；
  * 这里走 require_admin（整个 router 都挂），按 **管理员身份** 计额，
    不计 IP —— 管理员在后台批量导，不该被自己公司公网 IP 的限额卡住。

数据契约、单位、上限、分类枚举完全复用 kg/importer 与 kg/import_bundle：
  * 解析/校验 → importer.parse_table / validate_table
  * 落审核队列 → import_bundle.stage_bundles（source_key 仍走 excel_import，
    和网友上传同一队列，审核员在「补录审核」里看到的是同一类东西）
  * 批量通过 → promote.approve_bundle(merge_now=False) 逐条翻候选，
    最后 promote.merge_pending(batch_ids=[...]) 范围合并 —— 不会把历史
    无关的 approved 候选一起重处理。

为什么 import 和 approve 分开两步：
  预览阶段只写 kg_import_job / kg_import_row，不写 bundle —— 点错一次
  不会生成几百条脏草稿；管理员看清「有多少行、错在哪」再 commit，
  commit 之后才进审核队列，approve 是最后一道确认。
"""
from __future__ import annotations

import logging
import uuid
from typing import Any

import psycopg
from fastapi import APIRouter, Body, Depends, File, HTTPException, Query, UploadFile
from psycopg.types.json import Jsonb

import config
from api.deps import require_admin
from kg import importer, import_bundle, promote

import db

log = logging.getLogger("api.admin_import")

router = APIRouter(
    prefix="/admin/imports", tags=["admin-import"],
    dependencies=[Depends(require_admin)],
)

_TABLE_MISSING = "导入相关表不存在，请先运行 python -m scripts.migrate（012 / 013）"

# 管理员导入的每日上限：比公开的 20 次/天宽松，但仍防止把后台当批量接口刷
_ADMIN_DAILY_LIMIT = 100


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


def _enabled() -> None:
    if not config.FOOD_IMPORT_ENABLED:
        raise HTTPException(
            status_code=503,
            detail={"code": "import_disabled", "message": "导入功能暂时关闭"},
        )


def _bad(message: str, code: str = "bad_file", status: int = 400) -> HTTPException:
    return HTTPException(status_code=status, detail={"code": code, "message": message})


# ============================================================
# 模板 + 元信息（复用 importer 的契约）
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
            "daily_per_admin": _ADMIN_DAILY_LIMIT,
            "preview_ttl_min": config.FOOD_IMPORT_PREVIEW_TTL_MIN,
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
    }


# ============================================================
# 上传 → 解析 → 预览（不写 bundle）
# ============================================================
def _quota_used(admin_id: str) -> int:
    """这个管理员 24 小时内已经导过几次。"""
    with db.connect() as conn:
        _require_tables(conn)
        cur = conn.cursor()
        cur.execute(
            "SELECT count(*) FROM kg_import_job "
            "WHERE reviewer = %s AND created_at > now() - interval '24 hours'",
            (admin_id,),
        )
        return int(cur.fetchone()[0])


@router.post("/preview")
async def preview(
    file: UploadFile = File(...),
    admin: dict = Depends(require_admin),
):
    """上传 → 解析 → 校验 → 返回逐行结论。**不写 bundle。**"""
    _enabled()
    admin_id = admin["user_id"]

    if _quota_used(admin_id) >= _ADMIN_DAILY_LIMIT:
        raise HTTPException(
            status_code=429,
            detail={
                "code": "quota_exhausted",
                "message": f"今天已导入 {_ADMIN_DAILY_LIMIT} 次，明天再来或调高上限",
            },
        )

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
    summary = importer.summarize(results)
    job_id = uuid.uuid4()

    with db.connect() as conn:
        _require_tables(conn)
        cur = conn.cursor()
        cur.execute(
            "INSERT INTO kg_import_job (job_id, filename, client_ip, reviewer, "
            "template_version, total_rows, ok_rows, warn_rows, error_rows) "
            "VALUES (%s, %s, 'admin', %s, %s, %s, %s, %s, %s)",
            (
                job_id,
                (file.filename or "")[:200],
                admin_id,
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
        "管理员导入预览 job=%s admin=%s file=%s 共 %d 行，可提交 %d",
        job_id, admin_id, file.filename, summary["total"], summary["importable"],
    )
    return {
        "job_id": str(job_id),
        "filename": file.filename,
        "sheet": table.sheet,
        "template_version": importer.TEMPLATE_VERSION,
        "summary": summary,
        "unknown_headers": table.unknown_headers,
        "rows": [r.as_preview() for r in results],
    }


# ============================================================
# 提交审核（写 bundle，状态 pending_review）
# ============================================================
@router.post("/{job_id}/commit")
def commit(job_id: str, admin: dict = Depends(require_admin)):
    """把批次里可导入的行写成 pending_review 的 bundle。"""
    _enabled()
    admin_id = admin["user_id"]

    try:
        jid = uuid.UUID(job_id)
    except (ValueError, AttributeError) as exc:
        raise _bad("批次号格式不对", code="bad_job_id", status=404) from exc

    with db.connect() as conn:
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

    results = [
        importer.RowResult(
            row_no=row_no, name=name, verdict=verdict, messages=[], payload=payload
        )
        for row_no, name, verdict, payload in rows
    ]
    staged = import_bundle.stage_bundles(results, jid, filename or "")

    with db.connect() as conn:
        conn.execute(
            "UPDATE kg_import_job SET status = 'committed', committed_at = now() "
            "WHERE job_id = %s",
            (jid,),
        )

    created = sum(1 for x in staged if x["action"] == "created")
    updated = len(staged) - created
    log.info("管理员导入提交 job=%s admin=%s 新增 %d 覆盖 %d", job_id, admin_id, created, updated)
    return {
        "job_id": job_id,
        "filename": filename,
        "staged": len(staged),
        "inserted": created,
        "updated": updated,
        "items": staged,
    }


# ============================================================
# 批量通过：逐条翻候选 → 范围合并
# ============================================================
@router.post("/{job_id}/approve")
def approve_job(job_id: str, admin: dict = Depends(require_admin)):
    """整批通过该任务的全部 bundle 并合并进主图。

    只处理这一个 job 产出的 bundle；别的来源（网友上传 / 抓取）不受影响。
    """
    _enabled()
    admin_id = admin["user_id"]

    try:
        jid = uuid.UUID(job_id)
    except (ValueError, AttributeError) as exc:
        raise _bad("批次号格式不对", code="bad_job_id", status=404) from exc

    with db.connect() as conn:
        _require_tables(conn)
        cur = conn.cursor()
        cur.execute(
            "SELECT id, status FROM kg_food_bundle "
            "WHERE external_id LIKE %s ORDER BY id",
            (f"{job_id}#%",),
        )
        bundles = cur.fetchall()

    if not bundles:
        raise _bad("这个任务没有可通过的 bundle", code="nothing_to_approve", status=409)

    # 先逐条 approve（merge_now=False，只翻候选不写图），收集 batch_id
    batch_ids: list[str] = []
    approved = 0
    skipped: list[dict] = []
    for bundle_id, status in bundles:
        if status in ("merged", "rejected"):
            skipped.append({"bundle_id": bundle_id, "status": status})
            continue
        try:
            promote.approve_bundle(
                bundle_id, reviewer=admin_id, merge_now=False,
            )
            batch_ids.append(f"bundle:{bundle_id}")
            approved += 1
        except promote.PromoteError as exc:
            skipped.append({"bundle_id": bundle_id, "status": status, "error": str(exc)})

    # 范围合并：只碰这批 bundle 的候选
    merge_report: dict[str, Any] = {"nodes": 0, "edges": 0, "aliases": []}
    if batch_ids:
        merge_report = promote.merge_pending(batch_ids=batch_ids)

    log.info(
        "管理员批量通过 job=%s admin=%s 通过 %d 跳过 %d 合并节点 %d 边 %d",
        job_id, admin_id, approved, len(skipped),
        merge_report.get("nodes", 0), merge_report.get("edges", 0),
    )
    return {
        "job_id": job_id,
        "approved": approved,
        "skipped": skipped,
        "merge": merge_report,
    }


# ============================================================
# 导入历史 + 单任务详情
# ============================================================
@router.get("")
def list_jobs(
    limit: int = Query(20, ge=1, le=100),
    offset: int = Query(0, ge=0),
):
    with db.connect() as conn:
        _require_tables(conn)
        cur = conn.cursor()
        cur.execute(
            "SELECT job_id, filename, source_key, total_rows, ok_rows, warn_rows, "
            "error_rows, status, created_at, committed_at FROM kg_import_job "
            "ORDER BY created_at DESC LIMIT %s OFFSET %s",
            (limit, offset),
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
        cur.execute("SELECT count(*) FROM kg_import_job")
        total = int(cur.fetchone()[0])
    return {"count": total, "limit": limit, "offset": offset, "items": items}


@router.get("/{job_id}")
def get_job(job_id: str):
    try:
        jid = uuid.UUID(job_id)
    except (ValueError, AttributeError) as exc:
        raise _bad("批次号格式不对", code="bad_job_id", status=404) from exc

    with db.connect() as conn:
        _require_tables(conn)
        cur = conn.cursor()
        cur.execute(
            "SELECT job_id, filename, source_key, total_rows, ok_rows, warn_rows, "
            "error_rows, status, created_at, committed_at FROM kg_import_job "
            "WHERE job_id = %s",
            (jid,),
        )
        row = cur.fetchone()
        if row is None:
            raise _bad("没有这个导入批次", code="not_found", status=404)
        cols = [d[0] for d in cur.description]
        job = dict(zip(cols, row))
        job["job_id"] = str(job["job_id"])
        for k in ("created_at", "committed_at"):
            if job.get(k) is not None:
                job[k] = job[k].isoformat(timespec="seconds")

        cur.execute(
            "SELECT row_no, name, verdict, messages FROM kg_import_row "
            "WHERE job_id = %s ORDER BY row_no",
            (jid,),
        )
        rcols = [d[0] for d in cur.description]
        job["rows"] = [dict(zip(rcols, r)) for r in cur.fetchall()]
    return job
