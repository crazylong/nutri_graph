"""
kg/promote.py —— 审核闭环：bundle → 候选 → 主图（Phase 9.3）

为什么要有这一层，而不是「审核通过就直接写 Neo4j」：

  主图只能有**一个写入口**（kg.merge）。如果 approve 自己 Cypher 写一把，
  数据出问题时你无法回答「这条边是哪来的、哪个批次加的、能不能整批回滚」。
  所以这里只做「翻译」：把一个 bundle 翻译成节点候选（kg_node_candidate）
  和边候选（kg_candidate），真正写图交给 kg.merge。

状态机（两个方向都可回滚）：

  kg_food_bundle:  draft / verified / pending_review
                     ├── approve → approved ──merge──→ merged
                     └── reject  → rejected

  kg_node_candidate / kg_candidate:  pending → approved → merged
                                              └→ rejected

三个必须做对的细节：

  1. **同名不新建**：审核时如果「黑豆」已经挂在某个 canonical_id 上，
     就挂过去，不新建节点。否则「乌豆/黑大豆」会被建成四个节点 ——
     这正是 9.1 花力气做 near_miss 要防的事，不能在最后一步破功。
  2. **canonical_id 分配要幂等**：先查别名表、再查已审核的节点候选，
     都没有才 nextval。merge 失败重试时不会重复分配、留下孤儿节点。
  3. **别名必须在 merge 之后写**：先写别名后写图，会出现
     「能链到 cid 但 Neo4j 里没这个节点」的中间态，问题很难查。
"""
from __future__ import annotations

import logging
from typing import Any

import psycopg

import config
from kg import nutrients as nut_mod
from kg.normalize import normalize_text

import db

log = logging.getLogger("kg.promote")


class PromoteError(RuntimeError):
    """审核动作被拒绝（状态不对、有硬否决、缺字段等）。"""


# 这些属性一旦节点已存在就不许被新 bundle 覆盖（旧值为空时才补）。
# canonical_name / category 属于「身份」，别名命中时它们必须让位给既有节点，
# 否则「北豆腐」这行数据会把承载全部豆腐数据的节点改名。
_STICKY_PROPS: frozenset[str] = frozenset(
    {"summary", "provenance", "canonical_name", "category"}
)


def _as_float(v: Any) -> float | None:
    """bundle item 的 field_value 是 TEXT 存的。转不动就留 None ——

    None 的语义是「这条关系没有数值」，而不是 0（0 是「检出为零」，两回事）。
    """
    try:
        return float(str(v).strip())
    except (TypeError, ValueError):
        return None


# ============================================================
# 查询
# ============================================================
def list_bundles(status: str = "pending_review", limit: int = 20) -> list[dict[str, Any]]:
    with db.connect() as conn:
        cur = conn.cursor()
        cur.execute(
            "SELECT b.id, b.food_key, b.canonical_name, b.english_name, b.scientific_name, "
            "       b.category, b.source_desc, b.score_total, b.source_count, b.hard_veto, "
            "       b.status, b.canonical_id, b.created_at, b.source_key, "
            "       COALESCE(b.sensitive_hits, '[]'::jsonb) AS sensitive_hits "
            "FROM kg_food_bundle b WHERE b.status = %s ORDER BY b.id LIMIT %s",
            (status, limit),
        )
        cols = [d[0] for d in cur.description]
        rows = [dict(zip(cols, r)) for r in cur.fetchall()]
    # 列表只给摘要：有几个 block、几个待确认。审核员在列表上就能看出该先看哪条。
    for row in rows:
        hits = row.get("sensitive_hits") or []
        row["sensitive_block"] = sum(1 for h in hits if h.get("action") == "block")
        row["sensitive_review"] = sum(1 for h in hits if h.get("action") == "review")
    return rows


def get_bundle(bundle_id: int) -> dict[str, Any]:
    with db.connect() as conn:
        cur = conn.cursor()
        cur.execute(
            "SELECT id, request_id, food_key, canonical_name, english_name, scientific_name, "
            "       category, nature, aliases, source_key, external_id, source_desc, "
            "       evidence_type, score_total, score_detail, source_count, sources, "
            "       hard_veto, status, canonical_id, reviewer, reviewed_at, reject_reason, "
            "       COALESCE(sensitive_hits, '[]'::jsonb) AS sensitive_hits, "
            "       COALESCE(sensitive_word_version, 0) AS sensitive_word_version "
            "FROM kg_food_bundle WHERE id = %s",
            (bundle_id,),
        )
        row = cur.fetchone()
        if row is None:
            raise PromoteError(f"bundle {bundle_id} 不存在")
        cols = [d[0] for d in cur.description]
        bundle = dict(zip(cols, row))

        cur.execute(
            "SELECT id, item_type, field_name, field_value, unit, source_locator, "
            "       grounded, item_status FROM kg_food_bundle_item "
            "WHERE bundle_id = %s ORDER BY id",
            (bundle_id,),
        )
        icols = [d[0] for d in cur.description]
        bundle["items"] = [dict(zip(icols, r)) for r in cur.fetchall()]

        cur.execute(
            "SELECT node_type, canonical_id, name FROM kg_node_candidate "
            "WHERE bundle_id = %s ORDER BY id",
            (bundle_id,),
        )
        bundle["node_candidates"] = [
            {"node_type": r[0], "canonical_id": r[1], "name": r[2]} for r in cur.fetchall()
        ]

        # 备注只在 item 上，行上没有 note 列。来源文本在行的 source_desc
        # （导入时 payload.source 写在这里）。item 里同名的 source_desc 经常是空的，
        # 盖上去会把行上的「加微信」扫没。
        from kg.sensitive import BUNDLE_FIELDS, current_version, scan_obj

        scan_input = dict(bundle)
        notes = [
            it["field_value"]
            for it in bundle["items"]
            if it.get("item_type") == "field"
            and it.get("field_name") == "note"
            and it.get("field_value")
        ]
        if notes:
            scan_input["note"] = "\n".join(notes)
        live = [h.as_dict() for h in scan_obj(scan_input, BUNDLE_FIELDS)]
        stored = bundle.get("sensitive_hits") or []
        bundle["sensitive_hits_live"] = live
        bundle["hits_live"] = live
        bundle["hits_stored"] = stored
        bundle["word_version_live"] = current_version()
        bundle["word_version_stored"] = int(bundle.get("sensitive_word_version") or 0)
    return bundle


# ============================================================
# 身份判定：新节点 还是 挂到既有节点
# ============================================================
def _resolve_canonical_id(conn, names: list[str]) -> tuple[str, str]:
    """返回 (canonical_id, 依据)。有依据 = 不新建节点。"""
    norm = [n for n in dict.fromkeys(normalize_text(x) for x in names) if n]
    if not norm:
        return "", ""

    row = conn.execute(
        "SELECT canonical_id FROM dict_alias "
        "WHERE entity_type = 'Ingredient' AND alias_norm = ANY(%s) LIMIT 1",
        (norm,),
    ).fetchone()
    if row:
        return row[0], "existing_alias"

    # merge 失败后重试：此时别名还没写，但节点候选已经占了 id，必须复用它
    row = conn.execute(
        "SELECT canonical_id FROM kg_node_candidate "
        "WHERE node_type = 'Ingredient' AND name = ANY(%s) "
        "  AND status IN ('approved', 'merged') LIMIT 1",
        (norm,),
    ).fetchone()
    if row:
        return row[0], "existing_candidate"

    row = conn.execute(
        "SELECT 'ING_' || lpad(nextval('ingredient_cid_seq')::text, 5, '0')"
    ).fetchone()
    return row[0], ""


# ============================================================
# 审核：bundle → 候选
# ============================================================
def _upsert_ingredient_candidate(
    conn,
    batch_id: str,
    cid: str,
    name: str,
    props: dict[str, Any],
    aliases: list[str],
    bundle_id: int,
    reviewer: str,
) -> None:
    """写 Ingredient 节点候选。**已存在时是合并，不是覆盖。**

    原来这里 ON CONFLICT DO UPDATE props = EXCLUDED.props，等于用新 bundle
    的模板值整体替换掉旧属性：一个 curated 的 summary 会被
    「藜麦（谷物）」这种自动生成的一句话冲掉，别名也会被截断成新的三个。

    现在三条规则：
      * summary / provenance / canonical_name / category 只在旧值为空时才写入
        —— 它们是「这个节点是谁、怎么描述」的既有结论，新数据无权改写
      * 其他属性：新值非空才覆盖（空值表示「这张表没填」，不是「要清空」）
      * 别名取并集
      * 可信度只升不降：source_count 取大，provisional 取「与」

    为什么名字也要保住：dict_alias 里「豆腐 / 南豆腐 / 北豆腐 / 老豆腐」
    都指向 ING_00003。导入一行「北豆腐」如果直接把 canonical_name 改成
    北豆腐，这个节点在问答里就从「豆腐」变成了「北豆腐」，而它承载的是
    全部豆腐的数据 —— 显示名被一个别名劫持了。
    """
    old = conn.execute(
        "SELECT props, aliases FROM kg_node_candidate "
        "WHERE canonical_id = %s AND node_type = 'Ingredient'",
        (cid,),
    ).fetchone()

    if old is None:
        conn.execute(
            """
            INSERT INTO kg_node_candidate
                (batch_id, node_type, canonical_id, name, props, aliases, bundle_id,
                 status, reviewer, reviewed_at)
            VALUES (%s, 'Ingredient', %s, %s, %s, %s, %s, 'approved', %s, now())
            """,
            (
                batch_id, cid, name,
                psycopg.types.json.Jsonb(props), aliases, bundle_id, reviewer,
            ),
        )
        return

    old_props = dict(old[0] or {})
    merged = dict(old_props)
    for k, v in props.items():
        if k in _STICKY_PROPS:
            # 旧值非空就以旧值为准；旧值为空才补
            if old_props.get(k) in (None, "", [], {}):
                if v not in (None, "", [], {}):
                    merged[k] = v
            continue
        if v not in (None, "", [], {}):
            merged[k] = v
    merged["source_count"] = max(
        int(old_props.get("source_count") or 0), int(props.get("source_count") or 0)
    )
    merged["provisional"] = bool(old_props.get("provisional", False)) and bool(
        props.get("provisional", True)
    )
    merged_aliases = list(dict.fromkeys([*(old[1] or []), *aliases]))

    conn.execute(
        "UPDATE kg_node_candidate SET props = %s, aliases = %s, batch_id = %s, "
        "  bundle_id = %s, status = 'approved', reviewer = %s, reviewed_at = now() "
        "WHERE canonical_id = %s AND node_type = 'Ingredient'",
        (
            psycopg.types.json.Jsonb(merged), merged_aliases, batch_id,
            bundle_id, reviewer, cid,
        ),
    )


def _sensitive_gate(
    conn, bundle_id: int, obj: dict, *, confirm: bool, reviewer: str
) -> None:
    """bundle 批准口的敏感词硬闸。命中就转成 PromoteError（api/admin 已映射 409）。"""
    from kg.sensitive import SensitiveBlocked, assert_bundle_approvable

    try:
        assert_bundle_approvable(
            conn, bundle_id, obj, confirm=confirm, reviewer=reviewer
        )
    except SensitiveBlocked as exc:
        raise PromoteError(str(exc)) from exc


def approve_bundle(
    bundle_id: int,
    reviewer: str = "admin",
    item_ids: list[int] | None = None,
    merge_now: bool = True,
    confirm_sensitive: bool = False,
) -> dict[str, Any]:
    """通过一个 bundle：翻译成节点/边候选，并（默认）立即合并进主图。

    item_ids 传了就只采用这些项（逐条审核）；不传则采用除 rejected 之外的全部。
    """
    with db.connect() as conn:
        row = conn.execute(
            "SELECT food_key, canonical_name, english_name, scientific_name, category, "
            "       nature, source_desc, external_id, score_total, source_count, sources, "
            "       hard_veto, status, request_id, aliases, evidence_type, source_key "
            "FROM kg_food_bundle WHERE id = %s FOR UPDATE",
            (bundle_id,),
        ).fetchone()
        if row is None:
            raise PromoteError(f"bundle {bundle_id} 不存在")

        (
            food_key, canonical_name, english_name, scientific_name, category,
            nature, source_desc, external_id, score_total, source_count, sources,
            hard_veto, status, request_id, bundle_aliases, evidence_type, source_key,
        ) = row

        if status in ("rejected", "merged"):
            raise PromoteError(f"bundle 当前状态为 {status}，不能再通过")
        if hard_veto:
            # 硬否决意味着「这条数据本身有问题」（字段缺失 / 与现图冲突 / 同名异物），
            # 不是「分数低」。这种数据人工也不该直接放进主图，先驳回重抓。
            raise PromoteError(f"存在硬否决，不能通过：{hard_veto}")

        if item_ids:
            cur = conn.execute(
                "SELECT id, item_type, field_name, field_value, unit, source_locator "
                "FROM kg_food_bundle_item WHERE bundle_id = %s AND id = ANY(%s) "
                "  AND item_status <> 'rejected' ORDER BY id",
                (bundle_id, item_ids),
            )
        else:
            cur = conn.execute(
                "SELECT id, item_type, field_name, field_value, unit, source_locator "
                "FROM kg_food_bundle_item WHERE bundle_id = %s "
                "  AND item_status <> 'rejected' ORDER BY id",
                (bundle_id,),
            )
        items = cur.fetchall()

        fields = {r[2]: r[3] for r in items if r[1] == "field"}
        nutrient_items = [r for r in items if r[1] == "nutrient"]

        name = fields.get("canonical_name") or canonical_name
        if not name:
            raise PromoteError("缺少 canonical_name，无法建节点")
        if not (fields.get("category") or category):
            raise PromoteError("缺少 category，无法建节点")

        # ---------- 敏感词硬闸（步骤 2.3）----------
        # 入口过滤是"效率闸"，这里是"责任闸"：人签字之前后端必须自己再扫一遍，
        # 防的是"入口没拦住的存量数据被批准"和"审核员手滑 / 被社工"。
        _sensitive_gate(
            conn,
            bundle_id,
            {
                "canonical_name": name,
                "aliases": bundle_aliases or [],
                "english_name": english_name,
                "scientific_name": scientific_name,
                "source_desc": source_desc,
                # 导入 payload 的 note / source 在 items 上，不在 bundle 行里
                "note": fields.get("note", ""),
                "source": fields.get("source", ""),
            },
            confirm=confirm_sensitive,
            reviewer=reviewer,
        )

        # 别名也参与身份判定：导入表里写「北豆腐」，别名有「老豆腐」，
        # 若「老豆腐」已经挂在既有节点上，就该挂过去，而不是再建一个。
        cid, basis = _resolve_canonical_id(
            conn, [name, food_key, *(bundle_aliases or [])]
        )
        # 「过了人工审核」和「数值被交叉验证过」是两件事：
        # 前者解决「这是不是那个东西」，后者解决「这个数对不对」。
        # 只有单一来源时，内容可信但数值未经交叉验证 → 标 provisional，检索降级引用。
        provisional = source_count < config.FOOD_TRUSTED_MIN_SOURCES
        evidence_level = (
            config.PROVISIONAL_EVIDENCE_LEVEL if provisional else config.TRUSTED_EVIDENCE_LEVEL
        )
        batch_id = f"bundle:{bundle_id}"

        aliases = [
            a
            for a in dict.fromkeys([name, food_key, english_name, *(bundle_aliases or [])])
            if a
        ]

        # ---------- 节点候选 ----------
        ing_props: dict[str, Any] = {
            "canonical_name": name,
            "category": fields.get("category") or category,
            "aliases": aliases,
            "summary": f"{name}（{fields.get('category') or category}）",
            "source_count": source_count,
            "provisional": provisional,
            "provenance": f"bundle:{bundle_id} {source_key or 'unknown'}:{external_id}",
        }
        if fields.get("scientific_name") or scientific_name:
            ing_props["scientific_name"] = fields.get("scientific_name") or scientific_name
        if fields.get("nature") or nature:
            ing_props["nature"] = fields.get("nature") or nature

        _upsert_ingredient_candidate(
            conn,
            batch_id=batch_id,
            cid=cid,
            name=name,
            props=ing_props,
            aliases=aliases,
            bundle_id=bundle_id,
            reviewer=reviewer,
        )

        # ---------- 边候选 ----------
        edge_count = 0
        extra_nodes = 0
        for (_iid, _t, fname, fval, unit, locator) in nutrient_items:
            ref = nut_mod.resolve(fname, unit)
            if ref is None:
                log.info("营养素「%s %s」不在收录范围，跳过", fname, unit)
                continue

            # 不管新老都补一份节点候选：营养素有「共享词汇」的性质，
            # 种子里有 20 个，但主图不一定都建过（只建过被边引用到的）。
            # DO NOTHING 保证既有节点不被改写，缺的那个能顺手补上。
            inserted = conn.execute(
                """
                INSERT INTO kg_node_candidate
                    (batch_id, node_type, canonical_id, name, props, aliases, bundle_id,
                     status, reviewer, reviewed_at)
                VALUES (%s, 'Nutrient', %s, %s, %s, '{}', NULL, 'approved', %s, now())
                ON CONFLICT (canonical_id) DO NOTHING
                RETURNING id
                """,
                (
                    batch_id, ref.canonical_id, ref.name,
                    psycopg.types.json.Jsonb(
                        {"name": ref.name, "unit": ref.unit, "category": ref.category}
                    ),
                    reviewer,
                ),
            ).fetchone()
            if inserted and ref.is_new:
                extra_nodes += 1

            conn.execute(
                """
                INSERT INTO kg_candidate
                    (batch_id, src_type, src_id, rel, dst_type, dst_id, dst_name,
                     claim_text, evidence_type, evidence_level, sources, confidence,
                     source_locator, provisional, source_count, amount, unit,
                     status, reviewer, reviewed_at)
                VALUES (%s,'Ingredient',%s,'CONTAINS_NUTRIENT','Nutrient',%s,%s,
                        %s,%s,%s,%s,%s,%s,%s,%s,%s,%s,'approved',%s, now())
                ON CONFLICT (batch_id, src_id, rel, dst_id) DO UPDATE SET
                    claim_text = EXCLUDED.claim_text,
                    evidence_type = EXCLUDED.evidence_type,
                    evidence_level = EXCLUDED.evidence_level,
                    sources = EXCLUDED.sources,
                    confidence = EXCLUDED.confidence,
                    source_locator = EXCLUDED.source_locator,
                    provisional = EXCLUDED.provisional,
                    source_count = EXCLUDED.source_count,
                    amount = EXCLUDED.amount,
                    unit = EXCLUDED.unit,
                    status = 'approved',
                    reviewer = EXCLUDED.reviewer,
                    reviewed_at = now()
                """,
                (
                    batch_id, cid, ref.canonical_id, ref.name,
                    f"{name}每 100g 含{ref.name} {fval} {ref.unit}",
                    evidence_type or "measured",
                    evidence_level,
                    psycopg.types.json.Jsonb(sources or []),
                    min(1.0, float(score_total or 0.0)),
                    locator,
                    provisional,
                    source_count,
                    _as_float(fval),
                    ref.unit,
                    reviewer,
                ),
            )
            edge_count += 1

        conn.execute(
            "UPDATE kg_food_bundle SET status = 'approved', canonical_id = %s, "
            "reviewer = %s, reviewed_at = now() WHERE id = %s",
            (cid, reviewer, bundle_id),
        )

    result: dict[str, Any] = {
        "bundle_id": bundle_id,
        "canonical_id": cid,
        "identity": basis or "created",
        "provisional": provisional,
        "name": name,
        "nodes": 1 + extra_nodes,
        "edges": edge_count,
        "request_id": request_id,
    }

    if merge_now:
        result["merge"] = merge_pending(bundle_id)
    return result


def reject_bundle(bundle_id: int, reviewer: str = "admin", reason: str = "") -> dict[str, Any]:
    """驳回。同时把抓取任务和登记请求一起关掉 —— 否则它明天又被抓一遍。"""
    if not reason:
        raise PromoteError("驳回必须写原因（三个月后你会想知道为什么）")
    with db.connect() as conn:
        row = conn.execute(
            "SELECT request_id, status FROM kg_food_bundle WHERE id = %s FOR UPDATE",
            (bundle_id,),
        ).fetchone()
        if row is None:
            raise PromoteError(f"bundle {bundle_id} 不存在")
        request_id, status = row
        if status == "merged":
            raise PromoteError("已合并进主图的 bundle 不能直接驳回，请先回滚批次")

        conn.execute(
            "UPDATE kg_food_bundle SET status = 'rejected', reviewer = %s, "
            "reviewed_at = now(), reject_reason = %s WHERE id = %s",
            (reviewer, reason[:500], bundle_id),
        )
        conn.execute(
            "UPDATE kg_node_candidate SET status = 'rejected', reviewer = %s, reviewed_at = now() "
            "WHERE bundle_id = %s AND status = 'pending'",
            (reviewer, bundle_id),
        )
        if request_id:
            conn.execute(
                "UPDATE kg_ingredient_request SET status = 'rejected' WHERE id = %s",
                (request_id,),
            )
            # 负缓存：任务置 rejected 后 _claim 不会再捡它，不会天天重抓同一个错误词
            conn.execute(
                "UPDATE kg_fetch_task SET status = 'rejected' WHERE request_id = %s",
                (request_id,),
            )
    return {"bundle_id": bundle_id, "status": "rejected", "reason": reason}


# ============================================================
# 合并进主图 + 别名回流
# ============================================================
def sync_aliases(
    canonical_id: str,
    aliases: list[str],
    source: str = "",
    confidence: float = 0.9,
) -> dict[str, Any]:
    """把别名写进 dict_alias。

    绝不抢别人的别名：如果某个名字已经挂在另一个 canonical_id 上，
    跳过并记日志。这一步是「乌豆 / 黑大豆 不能变成第二个黑豆」的最后一道闸 ——
    宁可新食材查不到，也不能让同一个名字指向两个节点。
    """
    written, skipped = [], []
    norm_map = {normalize_text(a): a for a in aliases if normalize_text(a)}

    with db.connect() as conn:
        for norm, raw in norm_map.items():
            row = conn.execute(
                "SELECT canonical_id FROM dict_alias "
                "WHERE entity_type = 'Ingredient' AND alias_norm = %s "
                "  AND canonical_id <> %s LIMIT 1",
                (norm, canonical_id),
            ).fetchone()
            if row:
                skipped.append({"alias": raw, "occupied_by": row[0]})
                log.warning("别名「%s」已被 %s 占用，跳过", raw, row[0])
                continue
            conn.execute(
                "INSERT INTO dict_alias (entity_type, canonical_id, alias, alias_norm, "
                "source, confidence) VALUES ('Ingredient', %s, %s, %s, %s, %s) "
                "ON CONFLICT DO NOTHING",
                (canonical_id, raw, norm, source, confidence),
            )
            written.append(raw)

        if norm_map:
            conn.execute(
                "UPDATE dict_alias_candidate SET status = 'approved', "
                "suggested_canonical_id = %s "
                "WHERE entity_type = 'Ingredient' AND alias_norm = ANY(%s)",
                (canonical_id, list(norm_map)),
            )
    return {"written": written, "skipped": skipped}


def _index_into_milvus(canonical_id: str, aliases: list[str]) -> None:
    """审核合并后，把非 provisional 的实体写进 Milvus 向量兜底。

    provisional 判断：kg_node_candidate.props->>'provisional' == 'true' 时跳过。
    单源、未交叉验证的节点不进向量库 —— 见 merge_pending 的注释。
    Milvus 不可用时静默跳过（向量兜底是增强，不是主链路，不能因它阻断审核）。
    """
    try:
        with db.connect() as conn:
            row = conn.execute(
                "SELECT props->>'provisional', name FROM kg_node_candidate "
                "WHERE canonical_id = %s AND node_type = 'Ingredient'",
                (canonical_id,),
            ).fetchone()
        if row is None:
            return
        if str(row[0]).lower() == "true":
            log.info("provisional 节点 %s 跳过 Milvus 索引", canonical_id)
            return

        from rag.client import insert_entity
        from rag.embed import get_embeddings

        name = row[1] or canonical_id
        text = " ".join([name, *aliases])
        vec = get_embeddings().embed_query(text)
        insert_entity(
            canonical_id=canonical_id,
            label="Ingredient",
            name=name,
            text=text,
            embedding=vec,
        )
        log.info("实体 %s 已索引进 Milvus", canonical_id)
    except Exception as exc:  # noqa: BLE001
        log.warning("Milvus 索引失败（跳过）%s：%s", canonical_id, exc)


def merge_pending(
    bundle_id: int | None = None,
    batch_ids: list[str] | None = None,
) -> dict[str, Any]:
    """把 approved 候选合并进主图，然后写别名、推进状态。

    两种口径：
      * bundle_id 单独传 → 只合并 `bundle:{id}` 这一个批次（单条审核）；
      * batch_ids 传列表 → 按多个批次范围合并（批量导入通过），不会把
        历史无关的 approved 候选一起重处理一遍。
    两者都不传 → 全量合并（历史兜底，脚本/CLI 用）。
    """
    from kg import merge as merge_mod

    if batch_ids is not None:
        batch_list = [b for b in batch_ids if b]
        if not batch_list:
            return {"nodes": 0, "edges": 0, "aliases": []}
        # 单批次走精确匹配；多批次走 ANY(%s)，仍然只碰这几批
        nodes = merge_mod.merge_nodes(batch_ids=batch_list)
        edges = merge_mod.merge(batch_ids=batch_list)
    else:
        batch_id = f"bundle:{bundle_id}" if bundle_id is not None else None
        nodes = merge_mod.merge_nodes(batch_id)
        edges = merge_mod.merge(batch_id)

    # 别名必须在节点进图之后写
    merged_nodes = []
    with db.connect() as conn:
        if batch_ids is not None:
            rows = conn.execute(
                "SELECT canonical_id, aliases, bundle_id FROM kg_node_candidate "
                "WHERE batch_id = ANY(%s) AND node_type = 'Ingredient' AND status = 'merged'",
                (batch_list,),
            ).fetchall()
        elif bundle_id is not None:
            rows = conn.execute(
                "SELECT canonical_id, aliases, bundle_id FROM kg_node_candidate "
                "WHERE batch_id = %s AND node_type = 'Ingredient' AND status = 'merged'",
                (f"bundle:{bundle_id}",),
            ).fetchall()
        else:
            rows = conn.execute(
                "SELECT canonical_id, aliases, bundle_id FROM kg_node_candidate "
                "WHERE node_type = 'Ingredient' AND status = 'merged'"
            ).fetchall()
        merged_nodes = [{"canonical_id": r[0], "aliases": list(r[1] or []), "bundle_id": r[2]} for r in rows]

    alias_report = []
    for n in merged_nodes:
        rep = sync_aliases(
            n["canonical_id"], n["aliases"], source=f"bundle:{n['bundle_id']}"
        )
        alias_report.append({"canonical_id": n["canonical_id"], **rep})
        # 非 provisional 的 Ingredient 才索引进 Milvus ——
        # 单一来源、未交叉验证的节点不该进向量库，否则实体链接能链到它，
        # 未审核数据就通过向量兜底进了上下文（Cypher 的 provisional 过滤管不到这里）。
        _index_into_milvus(n["canonical_id"], n["aliases"])
        if n["bundle_id"]:
            with db.connect() as conn:
                conn.execute(
                    "UPDATE kg_food_bundle SET status = 'merged' WHERE id = %s",
                    (n["bundle_id"],),
                )
                conn.execute(
                    "UPDATE kg_ingredient_request r SET status = 'approved' "
                    "FROM kg_food_bundle b WHERE b.id = %s AND r.id = b.request_id",
                    (n["bundle_id"],),
                )

    # 批量场景下：把「只建了节点候选、没有新增合并节点」的那批 bundle 也推到 merged。
    # （比如整批都命中了既有节点，merge 后候选状态已 merged 但 bundle 还停在 approved。）
    if batch_ids is not None:
        with db.connect() as conn:
            conn.execute(
                "UPDATE kg_food_bundle SET status = 'merged' "
                "WHERE id IN (SELECT bundle_id FROM kg_node_candidate "
                "             WHERE batch_id = ANY(%s) AND status = 'merged') "
                "  AND status = 'approved'",
                (batch_list,),
            )
    elif bundle_id is not None and not merged_nodes:
        with db.connect() as conn:
            conn.execute(
                "UPDATE kg_food_bundle SET status = 'merged' WHERE id = %s AND status = 'approved'",
                (bundle_id,),
            )

    return {"nodes": nodes, "edges": edges, "aliases": alias_report}
