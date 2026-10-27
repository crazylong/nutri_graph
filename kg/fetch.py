"""
kg/fetch.py —— 「抓取 + 校验」流水线（Phase 9.2）

设计立场：这是**离线批量任务**，不在 /chat 的请求链路里。
  理由一：一次抓取要 2 次外部 API + 可能的模型调用，10~40 秒，塞进同步请求等于自杀
  理由二：抓取别人的网站/API 属于「建库」动作，跟「回答用户问题」是两件事，
          混在一起既不好限流也不好审计

一次处理的完整路径：
  claim(任务) → 生成检索画像(A) → USDA 检索 → 同名异物核对 → 拉详情
              → 打分与硬否决(B) → 落 bundle + 逐项 → 推进 request 状态

幂等：同一 food_key 只有一个 task（UNIQUE），重跑会覆盖同一个 bundle（UNIQUE request_id），
      不会堆出多份产物。
"""
from __future__ import annotations

import datetime as dt
import logging
from typing import Any

import psycopg

import config
from kg import draft as draft_mod
from kg import usda
from kg.usda import UsdaClient, UsdaError
from kg.verify import verify

log = logging.getLogger("kg.fetch")

MAX_ATTEMPTS = 3


# ============================================================
# 候选挑选：同名异物的核心防线
# ============================================================
def choose(draft: "draft_mod.FoodDraft", candidates: list[dict]) -> tuple[dict | None, str]:
    """从搜索结果里挑出「真的是这个东西」的那一条。

    两道判定，任何一条不过就否决这个候选：
      1. 描述里必须出现身份关键词（黑豆 → soybean）
      2. 描述里不能出现排除词（黑豆 → black / turtle …）

    为什么不能靠 foodCategory：实测「黑豆」和「黑菜豆」同属
    "Legumes and Legume Products"，分类完全一样，区分不了。
    """
    ident = [k.lower() for k in draft.identity_keywords if k]
    exclude = [k.lower() for k in draft.exclude_keywords if k]

    best: dict | None = None
    best_score = -1.0
    rejections: list[str] = []

    for cand in candidates:
        desc = (cand.get("description") or "")
        low = desc.lower()

        if ident and not any(k in low for k in ident):
            rejections.append(f"「{desc}」不含身份关键词 {'/'.join(ident)}")
            continue
        hit_bad = next((b for b in exclude if b in low), "")
        if hit_bad:
            rejections.append(f"「{desc}」疑似同名异物（含「{hit_bad}」）")
            continue

        score = 1.0
        if (cand.get("dataType") or "") == "SR Legacy":
            score += 0.2      # 传统参考数据，比 Foundation 覆盖面稳
        if "raw" in low:
            score += 0.1      # 未加工形态，与「每 100g」口径一致
        if draft.scientific_name:
            genus = draft.scientific_name.split()[0].lower()
            if genus and genus in low:
                score += 0.2
        if score > best_score:
            best_score, best = score, cand

    if best is not None:
        return best, ""
    return None, (rejections[0] if rejections else "搜索结果为空")


# ============================================================
# 任务领取 / 恢复 / 预算
# ============================================================
_CLAIM_SQL = """
UPDATE kg_fetch_task t
SET status = 'running', started_at = now(), attempts = t.attempts + 1
WHERE t.id = (
    SELECT id FROM kg_fetch_task
    WHERE status = 'queued'
      AND (next_retry_at IS NULL OR next_retry_at <= now())
      {id_filter}
    ORDER BY id
    LIMIT 1
    FOR UPDATE SKIP LOCKED
)
RETURNING t.id, t.request_id, t.food_key, t.attempts
"""


def budget_used_today() -> int:
    with psycopg.connect(config.PG_DSN) as conn:
        row = conn.execute(
            "SELECT count(*) FROM kg_fetch_task WHERE budget_day = CURRENT_DATE "
            "AND status IN ('running', 'done', 'failed')"
        ).fetchone()
    return int(row[0]) if row else 0


def recover_stale(minutes: int = 15) -> int:
    """进程被杀 / 崩溃后留下的 running 任务，重新排队。

    没有这一步，BackgroundTasks 一旦随进程退出，那些任务会永远卡在 running。
    """
    with psycopg.connect(config.PG_DSN) as conn:
        cur = conn.execute(
            "UPDATE kg_fetch_task SET status = 'queued' "
            "WHERE status = 'running' AND started_at < now() - (%s || ' minutes')::interval "
            "RETURNING id",
            (minutes,),
        )
        n = len(cur.fetchall())
    if n:
        log.warning("恢复 %d 个卡在 running 的抓取任务", n)
    return n


def _claim(task_id: int | None) -> dict | None:
    sql = _CLAIM_SQL.format(id_filter="AND id = %(task_id)s" if task_id else "")
    with psycopg.connect(config.PG_DSN) as conn:
        params = {"task_id": task_id} if task_id else {}
        row = conn.execute(sql, params).fetchone()
    if row is None:
        return None
    return {"id": row[0], "request_id": row[1], "food_key": row[2], "attempts": row[3]}


def _finish(task_id: int, status: str, error: str = "", retry_in_h: float | None = None) -> None:
    with psycopg.connect(config.PG_DSN) as conn:
        if retry_in_h is not None:
            conn.execute(
                "UPDATE kg_fetch_task SET status = %s, last_error = %s, "
                "next_retry_at = now() + (%s || ' hours')::interval WHERE id = %s",
                (status, error[:500], retry_in_h, task_id),
            )
        else:
            conn.execute(
                "UPDATE kg_fetch_task SET status = %s, last_error = %s, finished_at = now() "
                "WHERE id = %s",
                (status, error[:500], task_id),
            )


def _set_request_status(request_id: int, status: str) -> None:
    with psycopg.connect(config.PG_DSN) as conn:
        conn.execute(
            "UPDATE kg_ingredient_request SET status = %s WHERE id = %s",
            (status, request_id),
        )


# ============================================================
# 落库
# ============================================================
def _alias_conflict(draft: "draft_mod.FoodDraft", food_key: str) -> str:
    """同一名字已经在图谱里挂了别的 canonical_id → 冲突，必人工。"""
    from kg.normalize import AliasStore

    store = AliasStore(config.PG_DSN)
    for name in {draft.canonical_name, food_key}:
        if not name:
            continue
        hits = store.exact_lookup(name, entity_type="Ingredient")
        if hits:
            return f"{name} → {hits[0]['canonical_id']}"
    return ""


def _persist(
    task: dict,
    draft: "draft_mod.FoodDraft",
    pick: dict,
    detail: dict,
    nutrients: list[dict],
    result,
) -> int:
    source_key = "usda_fdc"
    fdc_id = str(detail.get("fdcId") or pick.get("fdcId") or "")
    desc = str(detail.get("description") or pick.get("description") or "")
    category = usda.category_description(detail) or draft.category
    sources = [
        {
            "type": "database",
            "title": "USDA FoodData Central",
            "publisher": "U.S. Department of Agriculture",
            "url": f"https://fdc.nal.usda.gov/food-details/{fdc_id}/nutrients",
            "external_id": fdc_id,
            "locator": desc,
        }
    ]

    with psycopg.connect(config.PG_DSN) as conn:
        # 重跑覆盖：先删旧 bundle（items 靠外键级联）
        conn.execute("DELETE FROM kg_food_bundle WHERE request_id = %s", (task["request_id"],))
        row = conn.execute(
            """
            INSERT INTO kg_food_bundle
                (request_id, food_key, canonical_name, english_name, scientific_name,
                 category, nature, source_key, external_id, source_desc,
                 score_total, score_detail, source_count, sources, hard_veto, status)
            VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
            RETURNING id
            """,
            (
                task["request_id"],
                task["food_key"],
                draft.canonical_name,
                draft.english_name,
                draft.scientific_name,
                category,
                "",                      # 性味不在 USDA，留空等离线导入
                source_key,
                fdc_id,
                desc,
                result.score_total,
                psycopg.types.json.Jsonb(result.score_detail),
                1,
                psycopg.types.json.Jsonb(sources),
                result.hard_veto,
                result.status,
            ),
        ).fetchone()
        bundle_id = row[0]

        items: list[tuple] = []
        for name, value in (
            ("canonical_name", draft.canonical_name),
            ("english_name", draft.english_name),
            ("scientific_name", draft.scientific_name),
            ("category", category),
            ("source_desc", desc),
        ):
            items.append(("field", name, str(value or ""), "", f"fdc:{fdc_id}", bool(value)))
        for n in nutrients:
            items.append(
                ("nutrient", n["name"], str(n["amount"]), n["unit"],
                 f"fdc:{fdc_id}#{n['nutrient_id']}", True)
            )
        if items:
            with conn.cursor() as cur:
                cur.executemany(
                    "INSERT INTO kg_food_bundle_item "
                    "(bundle_id, item_type, field_name, field_value, unit, source_locator, "
                    " grounded, item_status) VALUES (%s,%s,%s,%s,%s,%s,%s,%s)",
                    [(bundle_id, *it, "pending") for it in items],
                )
    return bundle_id


# ============================================================
# 主流程
# ============================================================
def process_task(task: dict, client: UsdaClient | None = None) -> dict:
    client = client or UsdaClient()
    food_key = task["food_key"]
    log.info("开始抓取 #%s %s（第 %d 次）", task["id"], food_key, task["attempts"])

    try:
        d = draft_mod.get_draft(food_key)
    except draft_mod.DraftError as exc:
        _finish(task["id"], "failed", f"画像生成失败：{exc}")
        return {"task_id": task["id"], "food_key": food_key, "status": "failed",
                "reason": f"画像生成失败：{exc}"}

    try:
        candidates = client.search(d.fdc_query)
        pick, veto = choose(d, candidates)
        if pick is None:
            msg = f"没有匹配到可信条目：{veto}"
            retry = task["attempts"] < MAX_ATTEMPTS
            _finish(task["id"], "queued" if retry else "failed", msg,
                    retry_in_h=0.5 * task["attempts"] if retry else None)
            return {"task_id": task["id"], "food_key": food_key, "status": "no_match",
                    "reason": msg}

        detail = client.get_food(pick["fdcId"])
        nutrients = usda.parse_nutrients(detail)

        vr = verify(
            draft=d.as_dict(),
            nutrients=nutrients,
            source_key="usda_fdc",
            source_count=1,
            match_veto="",
            conflict_alias=_alias_conflict(d, food_key),
        )
        bundle_id = _persist(task, d, pick, detail, nutrients, vr)
    except UsdaError as exc:
        retry = exc.retryable and task["attempts"] < MAX_ATTEMPTS
        _finish(task["id"], "queued" if retry else "failed", str(exc),
                retry_in_h=0.5 * task["attempts"] if retry else None)
        return {"task_id": task["id"], "food_key": food_key, "status": "error",
                "reason": str(exc), "retry": retry}
    except Exception as exc:  # noqa: BLE001 —— 单个任务失败不能带崩整批
        log.exception("抓取任务 #%s 异常", task["id"])
        _finish(task["id"], "failed", str(exc))
        return {"task_id": task["id"], "food_key": food_key, "status": "failed",
                "reason": str(exc)[:200]}

    _finish(task["id"], "done")
    _set_request_status(task["request_id"], "pending_review" if not vr.auto_approved else "approved")
    log.info(
        "抓取完成 #%s %s → %s（score=%s status=%s veto=%s）",
        task["id"], food_key, pick.get("description"), vr.score_total, vr.status,
        vr.hard_veto[:60],
    )
    return {
        "task_id": task["id"],
        "food_key": food_key,
        "status": "done",
        "bundle_id": bundle_id,
        "matched": pick.get("description"),
        "score": vr.score_total,
        "veto": vr.hard_veto,
        "bundle_status": vr.status,
        "nutrients": len(nutrients),
    }


def run(limit: int = 5, task_id: int | None = None, client: UsdaClient | None = None) -> list[dict]:
    """跑一批任务。预算是硬闸：超了就不再 claim。"""
    if task_id is None:
        used = budget_used_today()
        if used >= config.FOOD_FETCH_DAILY_BUDGET:
            log.warning("今日抓取预算已用尽（%d/%d）", used, config.FOOD_FETCH_DAILY_BUDGET)
            return [{"status": "budget_exhausted", "used": used}]

    results = []
    for _ in range(limit):
        task = _claim(task_id)
        if task is None:
            break
        results.append(process_task(task, client=client))
        if task_id:
            break
    return results
