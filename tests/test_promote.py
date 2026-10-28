"""
tests/test_promote.py —— 9.3 审核闭环

覆盖三件最容易出错的事：
  1. bundle 翻译成「节点 + 边」候选时，营养素映射对、能量不重复、证据等级封顶
  2. 同名不新建（黑大豆已经存在就必须挂过去，不能造第二个节点）
  3. provisional 数据的检索行为（默认可见但降级，严格模式排除）

需要 PostgreSQL + Neo4j；任一不可达就跳过，不红一片。
"""
from __future__ import annotations

import itertools

import psycopg
import pytest

import config
from kg import promote
from kg.query import GraphQuery
from tests.helpers import requires_neo4j, requires_pg

# graph：会写 Neo4j。默认被 conftest._graph_guard 跳过 ——
# Neo4j 社区版只有一个库，测试和生产共用同一张图，这类用例必须在
# 「确认这张图可以被清」时显式 ALLOW_GRAPH_TESTS=1 才跑。
pytestmark = [requires_pg, requires_neo4j, pytest.mark.graph]

_seq = itertools.count(1)
_created: list[tuple[int, int]] = []      # (bundle_id, request_id)


# ============================================================
# 造数据 / 清理
# ============================================================
def _make_bundle(
    label: str = "黑豆",
    nutrients: list[tuple[str, str, str, str]] | None = None,
    source_count: int = 1,
    hard_veto: str = "",
    exact_name: str = "",
) -> int:
    """造一份「已经抓完、等审核」的 bundle。

    名字默认带 ZTEST 前缀便于清理；exact_name 用于「必须和新名字完全一致」的用例
    （比如验证「黑大豆已存在 → 挂过去而不是新建」）。
    """
    n = next(_seq)
    name = exact_name or f"ZTEST{label}{n}"
    food_key = f"{name}"
    # 英文名也带前缀：否则每条用例都去抢同一个 "soybeans" 别名，
    # 第一次成功、后面全被拒，报告里全是「已占用」的噪音。
    english = f"ztestsoy{n}"
    nutrients = nutrients if nutrients is not None else [
        ("蛋白质", "36.49", "g", "fdc:174270#1003"),
        ("膳食纤维", "9.30", "g", "fdc:174270#1079"),
        ("钠", "2.00", "mg", "fdc:174270#1093"),
    ]

    with psycopg.connect(config.PG_DSN) as conn:
        rid = conn.execute(
            "INSERT INTO kg_ingredient_request "
            "(food_key, raw_mention, normalized, requested_by, source, status) "
            "VALUES (%s, %s, %s, 'tester', 'chat', 'pending_review') RETURNING id",
            (food_key, name, name),
        ).fetchone()[0]
        conn.execute(
            "INSERT INTO kg_fetch_task (request_id, food_key, status, attempts) "
            "VALUES (%s, %s, 'done', 1)",
            (rid, food_key),
        )
        bid = conn.execute(
            """
            INSERT INTO kg_food_bundle
                (request_id, food_key, canonical_name, english_name, scientific_name,
                 category, source_key, external_id, source_desc, score_total, score_detail,
                 source_count, sources, hard_veto, status)
            VALUES (%s,%s,%s,%s,%s,'豆类','usda_fdc','174270','Soybeans, mature seeds, raw',
                    0.88,'{}',%s,%s,%s,'pending_review')
            RETURNING id
            """,
            (
                rid, food_key, name, english, "Glycine max", source_count,
                psycopg.types.json.Jsonb(
                    [{"type": "database", "title": "USDA FoodData Central",
                      "url": "https://fdc.nal.usda.gov/x"}]
                ),
                hard_veto,
            ),
        ).fetchone()[0]

        items = [
            ("field", "canonical_name", name, "", "fdc:174270"),
            ("field", "category", "豆类", "", "fdc:174270"),
        ]
        for n in nutrients:
            items.append(("nutrient", n[0], n[1], n[2], n[3]))
        with conn.cursor() as cur:
            cur.executemany(
                "INSERT INTO kg_food_bundle_item (bundle_id, item_type, field_name, "
                "field_value, unit, source_locator, grounded, item_status) "
                "VALUES (%s,%s,%s,%s,%s,%s,true,'pending')",
                [(bid, *it) for it in items],
            )

    _created.append((bid, rid))
    return bid


@pytest.fixture(autouse=True)
def _isolate():
    yield
    if not _created:
        return

    # 先把本用例真正落到的 canonical_id 查出来 —— 清理必须**按它精确删**。
    # 不能再用 rollback(f"bundle:{bid}")：那个函数以前用 provenance CONTAINS
    # 匹配，测试库里 bid=12 会命中生产图里的 bundle:120…129 / bundle:1200…1299，
    # 一次清理删掉一百多个线上食材节点（2026-10-28 实际发生）。
    batches = [f"bundle:{bid}" for bid, _ in _created]
    cids: list[str] = []
    with psycopg.connect(config.PG_DSN) as conn:
        for bid, _rid in _created:
            row = conn.execute(
                "SELECT canonical_id FROM kg_food_bundle WHERE id = %s", (bid,)
            ).fetchone()
            if row and row[0]:
                cids.append(row[0])

    try:
        from neo4j import GraphDatabase

        driver = GraphDatabase.driver(
            config.NEO4J_URI, auth=(config.NEO4J_USER, config.NEO4J_PASSWORD)
        )
        with driver.session() as s:
            # 边：batch_id 是精确字符串，安全
            s.run(
                "MATCH ()-[r]->() WHERE r.batch_id IN $batches DELETE r", batches=batches
            ).consume()
            # 节点：只删「cid 匹配且 provenance 属于本批次」的，双重限定
            if cids:
                s.run(
                    "MATCH (n) WHERE n.canonical_id IN $cids AND n.provenance IN $batches "
                    "DETACH DELETE n",
                    cids=cids, batches=batches,
                ).consume()
            # 测试里新建的营养素节点（无 provenance，上面两个条件都不命中）
            s.run(
                "MATCH (n) WHERE n.canonical_id IN ['NUT_00021','NUT_00022'] "
                "AND NOT (n)<-[]-() DELETE n"
            ).consume()
        driver.close()
    except Exception:  # noqa: BLE001 —— 图不可用时不该让清理把用例搞红
        pass

    with psycopg.connect(config.PG_DSN) as conn:
        for bid, rid in _created:
            conn.execute("DELETE FROM kg_candidate WHERE batch_id = %s", (f"bundle:{bid}",))
            conn.execute("DELETE FROM kg_node_candidate WHERE batch_id = %s", (f"bundle:{bid}",))
            conn.execute("DELETE FROM kg_food_bundle_item WHERE bundle_id = %s", (bid,))
            conn.execute("DELETE FROM kg_food_bundle WHERE id = %s", (bid,))
            conn.execute("DELETE FROM kg_fetch_task WHERE request_id = %s", (rid,))
            conn.execute("DELETE FROM kg_ingredient_request WHERE id = %s", (rid,))
        conn.execute("DELETE FROM dict_alias WHERE alias_norm ILIKE 'ztest%'")
        conn.execute("DELETE FROM dict_alias WHERE canonical_id LIKE 'ING_TEST%'")
        conn.execute("DELETE FROM dict_alias_candidate WHERE alias_norm ILIKE 'ztest%'")
        # 「黑大豆」是 identity 用例里唯一用到的真实词，审核时 sync_aliases 会把它的
        # 候选行标成 approved。不清理会污染 test_food_request 的同名断言。
        conn.execute(
            "DELETE FROM dict_alias_candidate WHERE alias_norm = %s", ("黑大豆",)
        )
    _created.clear()


def _edges(bid: int) -> list[tuple]:
    with psycopg.connect(config.PG_DSN) as conn:
        return conn.execute(
            "SELECT dst_id, evidence_level, provisional, source_count, claim_text, "
            "       amount, unit "
            "FROM kg_candidate WHERE batch_id = %s ORDER BY dst_id",
            (f"bundle:{bid}",),
        ).fetchall()


def _neo4j_amount(cid: str, nid: str) -> tuple:
    """营养边上的 amount / unit —— 表格和前端优先读这两个属性。"""
    from neo4j import GraphDatabase

    driver = GraphDatabase.driver(
        config.NEO4J_URI, auth=(config.NEO4J_USER, config.NEO4J_PASSWORD)
    )
    try:
        with driver.session() as s:
            row = s.run(
                "MATCH (a {canonical_id: $cid})-[r:CONTAINS_NUTRIENT]->"
                "(n {canonical_id: $nid}) RETURN r.amount AS a, r.unit AS u",
                cid=cid, nid=nid,
            ).single()
            return (row["a"], row["u"]) if row else (None, None)
    finally:
        driver.close()


def _neo4j_edges(cid: str) -> int:
    from neo4j import GraphDatabase

    driver = GraphDatabase.driver(
        config.NEO4J_URI, auth=(config.NEO4J_USER, config.NEO4J_PASSWORD)
    )
    try:
        with driver.session() as s:
            return s.run(
                "MATCH (a {canonical_id: $cid})-[r]->() RETURN count(r) AS n", cid=cid
            ).single()["n"]
    finally:
        driver.close()


# ============================================================
# 翻译：bundle → 节点 + 边
# ============================================================
def test_approve_builds_nodes_and_edges():
    bid = _make_bundle()
    r = promote.approve_bundle(bid, reviewer="tester")

    assert r["identity"] == "created"
    assert r["canonical_id"].startswith("ING_0")
    assert r["nodes"] == 2          # Ingredient + 钠（图里原本没有的营养素）
    assert r["edges"] == 3
    assert r["provisional"] is True

    rows = _edges(bid)
    assert {x[0] for x in rows} == {"NUT_00012", "NUT_00011", "NUT_00022"}
    # 单源 → 证据等级封顶 C，且标 provisional
    assert all(x[1] == config.PROVISIONAL_EVIDENCE_LEVEL for x in rows)
    assert all(x[2] is True for x in rows)
    assert all(x[3] == 1 for x in rows)
    # 数值必须写进 claim_text（给人看的断言原文）
    protein = next(x for x in rows if x[0] == "NUT_00012")
    assert "36.49" in protein[4]
    # 同时要落到边的 amount / unit 上：前端表格和「哪个食材含铁更高」这类比较
    # 不该再去正则解析那句中文（%E 这种单位正则根本抓不准）
    assert (protein[5], protein[6]) == (36.49, "g")
    assert _neo4j_amount(r["canonical_id"], "NUT_00012") == (36.49, "g")


def test_bundle_marked_merged_and_request_approved():
    bid = _make_bundle()
    cid = promote.approve_bundle(bid)["canonical_id"]

    b = promote.get_bundle(bid)
    assert b["status"] == "merged"
    assert b["canonical_id"] == cid

    with psycopg.connect(config.PG_DSN) as conn:
        status = conn.execute(
            "SELECT r.status FROM kg_ingredient_request r "
            "JOIN kg_food_bundle b ON b.request_id = r.id WHERE b.id = %s",
            (bid,),
        ).fetchone()[0]
        alias = conn.execute(
            "SELECT canonical_id FROM dict_alias WHERE alias_norm = %s", (b["canonical_name"],)
        ).fetchone()
    assert status == "approved"
    # 别名必须在 merge 之后写进去，否则实体链接永远找不到这个新食材
    assert alias and alias[0] == cid


def test_energy_kj_not_duplicated():
    """「能量」有 kcal 和 kJ 两条，图里只能留一条，否则上下文自相矛盾。"""
    bid = _make_bundle(
        nutrients=[
            ("能量", "446", "kcal", "fdc:174270#1008"),
            ("能量", "1880", "kJ", "fdc:174270#1062"),
        ]
    )
    r = promote.approve_bundle(bid)
    rows = _edges(bid)
    assert r["edges"] == 1
    assert rows[0][0] == "NUT_00021"


def test_unknown_nutrient_skipped():
    bid = _make_bundle(nutrients=[("维生素B12", "0", "μg", "fdc:174270#1178")])
    r = promote.approve_bundle(bid)
    assert r["edges"] == 0


# ============================================================
# 身份判定：同名不新建
# ============================================================
def test_existing_alias_links_instead_of_creating():
    """「黑大豆」已经在图谱里时，新抓的「黑大豆」必须挂过去，不能造第二个节点。"""
    with psycopg.connect(config.PG_DSN) as conn:
        conn.execute(
            "INSERT INTO dict_alias (entity_type, canonical_id, alias, alias_norm, "
            "source, confidence) VALUES ('Ingredient','ING_TEST_BLACKSOY','黑大豆','黑大豆',"
            "'seed',1.0) ON CONFLICT DO NOTHING"
        )

    bid = _make_bundle(exact_name="黑大豆")
    r = promote.approve_bundle(bid)
    assert r["canonical_id"] == "ING_TEST_BLACKSOY"
    assert r["identity"] == "existing_alias"


def test_alias_is_never_stolen():
    with psycopg.connect(config.PG_DSN) as conn:
        conn.execute(
            "INSERT INTO dict_alias (entity_type, canonical_id, alias, alias_norm, "
            "source, confidence) VALUES ('Ingredient','ING_TEST_OWNER','ZTEST抢名','ZTEST抢名',"
            "'seed',1.0) ON CONFLICT DO NOTHING"
        )
    rep = promote.sync_aliases("ING_TEST_NEW", ["ZTEST抢名"])
    assert rep["written"] == []
    assert rep["skipped"][0]["occupied_by"] == "ING_TEST_OWNER"


# ============================================================
# 状态机
# ============================================================
def test_hard_veto_blocks_approval():
    bid = _make_bundle(hard_veto="与图谱既有条目冲突：黑豆 → ING_00031")
    with pytest.raises(promote.PromoteError, match="硬否决"):
        promote.approve_bundle(bid)


def test_reject_closes_request_and_task():
    bid = _make_bundle()
    promote.reject_bundle(bid, reviewer="tester", reason="物种不对，是黑菜豆")

    b = promote.get_bundle(bid)
    assert b["status"] == "rejected"
    assert "黑菜豆" in b["reject_reason"]

    with psycopg.connect(config.PG_DSN) as conn:
        req = conn.execute(
            "SELECT r.status, t.status FROM kg_ingredient_request r "
            "JOIN kg_fetch_task t ON t.request_id = r.id WHERE r.id = %s",
            (b["request_id"],),
        ).fetchone()
    # 请求与任务一起关掉，否则这个错误词每天都会被重抓一遍
    assert req == ("rejected", "rejected")


def test_reject_requires_reason():
    bid = _make_bundle()
    with pytest.raises(promote.PromoteError, match="原因"):
        promote.reject_bundle(bid, reason="")


def test_approve_twice_is_rejected():
    bid = _make_bundle()
    promote.approve_bundle(bid)
    with pytest.raises(promote.PromoteError, match="不能再通过"):
        promote.approve_bundle(bid)


# ============================================================
# 合并幂等 & provisional 检索行为
# ============================================================
def test_merge_is_idempotent():
    bid = _make_bundle()
    cid = promote.approve_bundle(bid, merge_now=False)["canonical_id"]

    first = promote.merge_pending(bid)
    n1 = _neo4j_edges(cid)
    second = promote.merge_pending(bid)
    n2 = _neo4j_edges(cid)

    assert first["edges"] == 3
    assert second["edges"] == 0        # 已经是 merged，不再重复合并
    assert n1 == n2 == 3


def test_provisional_hidden_in_strict_mode():
    bid = _make_bundle()
    cid = promote.approve_bundle(bid)["canonical_id"]

    q = GraphQuery()
    strict = q.run("nutrition", cid, ["measured"], include_provisional=False)
    loose = q.run("nutrition", cid, ["measured"], include_provisional=True)

    assert strict == []
    assert len(loose) == 3
    assert all(r["provisional"] for r in loose)


def test_multi_source_is_not_provisional():
    """接上第二个来源后同样的代码会自动放开 —— 不需要改一行逻辑。"""
    bid = _make_bundle(source_count=config.FOOD_TRUSTED_MIN_SOURCES)
    r = promote.approve_bundle(bid)
    assert r["provisional"] is False
    assert all(x[2] is False for x in _edges(bid))
