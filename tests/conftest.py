"""
tests/conftest.py —— pytest 基线

跑法：
  PG_DSN=postgresql://... DEBUG=true pytest -q

没有可达的 PostgreSQL 时全部跳过（本机没起库也不会红一片）。
"""
from __future__ import annotations

import os

# 必须在 import app / config 之前设好：load_dotenv 是 override=False，
# 所以这里的环境变量优先级高于 .env
os.environ["DEBUG"] = "true"
os.environ.setdefault("SMS_PROVIDER", "console")
# 基线跑「不强制登录」模式：这套用例大部分是直接打 /chat 的。
# 部署环境的 .env 可能是 AUTH_REQUIRED=true（生产就该是），
# 若不在这里钉死，同一份代码在开发机全绿、到服务器上就红一片 ——
# 那是环境差异，不是回归。强制模式由 test_auth_required_blocks_fake_uid_and_anon
# 用 monkeypatch 单独覆盖。
# 注意用 setdefault：外部显式给 AUTH_REQUIRED=true 时仍然尊重（用于手工验证）。
os.environ.setdefault("AUTH_REQUIRED", "false")
# 测试要连发很多条码，把基线额度放宽；频控用例自己用 monkeypatch 收紧
os.environ.setdefault("SMS_MAX_PER_IP_HOUR", "1000")
os.environ.setdefault("SMS_MAX_PER_PHONE_HOUR", "1000")
os.environ.setdefault("SMS_MAX_PER_PHONE_DAY", "1000")
os.environ.setdefault("SMS_MAX_PER_DEVICE_DAY", "1000")
os.environ.setdefault("SMS_MAX_GLOBAL_DAY", "10000")
# 图形验证码：基线放宽到「几乎永不触发」，图形码用例自己用 monkeypatch 收紧
os.environ.setdefault("CAPTCHA_FREE_SENDS_PER_IP", "100000")
# 测试用固定密钥：真实密钥只存在于 .env（.env 不存在时也能跑）
os.environ.setdefault("JWT_SECRET", "test-only-secret-0123456789abcdef0123456789abcdef")
os.environ.setdefault("PHONE_HASH_PEPPER", "test-only-pepper")
os.environ.setdefault(
    "PHONE_ENC_KEY", "AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA="
)

import pytest  # noqa: E402

from tests.helpers import PG_OK  # noqa: E402

# init_pg 的 DDL 里有 pgvector 扩展，测试库未必装得了，这里只补登录链路用到的两张表
_MEMORY_DDL = [
    """
    CREATE TABLE IF NOT EXISTS session (
        id         UUID PRIMARY KEY DEFAULT gen_random_uuid(),
        user_id    TEXT NOT NULL,
        started_at TIMESTAMPTZ DEFAULT now(),
        ended_at   TIMESTAMPTZ,
        summary    TEXT DEFAULT ''
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS user_profile (
        user_id    TEXT PRIMARY KEY,
        allergies  TEXT[] DEFAULT '{}',
        conditions TEXT[] DEFAULT '{}',
        diet_style TEXT DEFAULT '',
        avoid_list TEXT[] DEFAULT '{}',
        updated_at TIMESTAMPTZ DEFAULT now()
    )
    """,
]


@pytest.fixture(autouse=True)
def _fresh_alias_cache():
    """别名表是进程级单例。每条用例开始前丢掉，避免上一条插进 dict_alias 的行被缓存挡住。"""
    import kg.normalize as normalize

    normalize._alias_store = None
    yield


def pytest_configure(config):
    config.addinivalue_line(
        "markers", "no_db: 纯逻辑用例，不连 PostgreSQL / Neo4j，也不受「非测试库」保护的影响"
    )
    config.addinivalue_line(
        "markers",
        "graph: 会**写** Neo4j 的用例（审核 → 合并 → 回滚）。默认跳过，见 _graph_guard。",
    )


@pytest.fixture(autouse=True)
def _graph_guard(request):
    """默认不跑「写图」的用例。

    为什么单独一道闸：PG 可以指到 *_test 库，Neo4j 却**只有一个库**（社区版），
    测试和生产共用同一张图。写图的用例会：
      * 用同一个 ingredient_cid_seq 造节点 → 可能正好 MERGE 到线上已有节点上；
      * 清理时按 batch_id 删节点 → 一旦匹配写松了就是删生产数据。
    2026-10-28 就真删掉了线上 120 个食材节点（`provenance CONTAINS 'bundle:12'`
    命中了 bundle:120…129、bundle:1200…1299）。

    所以要跑这类用例，必须显式确认「这张图可以被清」：
        ALLOW_GRAPH_TESTS=1 pytest -q
    """
    if "graph" in request.keywords and os.environ.get("ALLOW_GRAPH_TESTS") != "1":
        pytest.skip(
            "写图的用例默认跳过：Neo4j 社区版只有一个库，测试与生产共用同一张图。"
            "确认这张图可以被写/被清，再设 ALLOW_GRAPH_TESTS=1。"
        )


@pytest.fixture(autouse=True)
def _destructive_guard(request):
    """拦住「对着有数据的库跑测试」这件事。

    这套用例会 TRUNCATE 业务表（kg_food_bundle / kg_ingredient_request / app_user …），
    但**清不到 Neo4j 和 dict_alias**。在对外的部署库上跑一遍的结果是：
    图里有「黑豆」这个节点、别名表也指着它，PG 里却查不到它是谁、什么时候、
    依据什么审核进来的 —— 审计链直接断掉，而这是整个补录设计最不能破的东西。

    真踩过一次：部署验收时顺手跑了 pytest，回头 /admin/stats 的已合并产物变成 0，
    而黑豆还在图上正常回答。

    所以：库名带 test 才放行，否则必须显式给 TEST_DB_OK=1。

    标了 no_db 的用例（纯逻辑，一行 SQL 都不发）直接放行 ——
    否则「本机连着部署库」会把这类用例一起跳过，等于没跑。
    """
    if "no_db" in request.keywords:
        return
    if not PG_OK:
        return
    import config

    db_name = config.PG_DSN.rsplit("/", 1)[-1].split("?")[0]
    if "test" in db_name or os.environ.get("TEST_DB_OK") == "1":
        return
    pytest.skip(
        f"目标库 `{db_name}` 看着不是测试库：本套用例会 TRUNCATE 业务表，"
        "而且清不到 Neo4j / dict_alias，会在图里留下没有审计记录的数据。"
        "确认可以清就设 TEST_DB_OK=1；更稳的做法是指一个独立的 *_test 库。"
    )


@pytest.fixture(scope="session")
def schema():
    """跑一遍迁移 + 补登录链路用到的表。"""
    if not PG_OK:
        pytest.skip("PostgreSQL 不可达")
    from scripts import migrate

    migrate.run()

    import psycopg

    import config

    with psycopg.connect(config.PG_DSN, autocommit=True) as conn:
        for ddl in _MEMORY_DDL:
            conn.execute(ddl)
    yield


@pytest.fixture(scope="session")
def client(schema):
    from fastapi.testclient import TestClient

    from app import app

    with TestClient(app) as c:
        yield c
