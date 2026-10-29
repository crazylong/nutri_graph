"""tests/test_db_pool.py —— 连接池（需求 3）

四条用例对应计划步骤 3.6，第一条最重要：

  ① 写了真的落库 —— 防的是"事务语义反转"。
     `with psycopg.connect()` 退出是提交，SQLAlchemy 的 `with engine.connect()` 退出是
     回滚。换成池以后少一次显式 commit，全站写操作会静默失效，而且现有测试大多
     自己 commit，不一定能发现。这条用例就是那道闸。

  ② 并发下连接数有上限 —— 池真的生效，不是每请求一条新连接。
  ③ 连接被掐断后能自愈 —— pre_ping 生效（容器空闲断连是必然发生的）。
  ④ 池打满返回 503 + pool_busy —— 不是悬挂到超时后 500。

用独立的 *_test 库跑：PG_DSN 指向 nutri_graph_test。
"""
from __future__ import annotations

import threading
import time

import pytest

import config

from tests.helpers import PG_OK, requires_pg

pytestmark = requires_pg


@pytest.fixture
def _isolated_pool(monkeypatch):
    """把全局池换成小池，用完一定还原。

    为什么必须隔离：db._engine 是模块级单例，别的用例共用同一个池。
    把池调小后不还原，后续用例会莫名其妙地拿不到连接。
    """
    import db

    saved = (config.PG_POOL_SIZE, config.PG_MAX_OVERFLOW, config.PG_POOL_TIMEOUT)
    yield monkeypatch
    monkeypatch.setattr(config, "PG_POOL_SIZE", saved[0])
    monkeypatch.setattr(config, "PG_MAX_OVERFLOW", saved[1])
    monkeypatch.setattr(config, "PG_POOL_TIMEOUT", saved[2])
    db.dispose()


def _activity_count() -> int:
    """当前库上的连接数（含本条查询自己）。"""
    import psycopg

    with psycopg.connect(config.PG_DSN, connect_timeout=3) as conn:
        return int(
            conn.execute(
                "SELECT count(*) FROM pg_stat_activity WHERE datname = current_database()"
            ).fetchone()[0]
        )


# ---------------------------------------------------------------
# ① 写了真的落库
# ---------------------------------------------------------------
def test_pool_write_is_committed_and_visible_from_another_connection():
    import db

    table = "pool_write_probe"
    with db.connect() as conn:
        conn.execute(f"DROP TABLE IF EXISTS {table}")
        conn.execute(f"CREATE TABLE {table} (v TEXT)")
        conn.execute(f"INSERT INTO {table} VALUES ('from-pool')")

    # 刻意换一条连接读：同一条连接读得到只能证明"没报错"，证明不了提交。
    import psycopg

    with psycopg.connect(config.PG_DSN) as conn:
        row = conn.execute(f"SELECT v FROM {table}").fetchone()
    assert row == ("from-pool",), (
        "池里的写操作没有提交到别的连接可见 —— "
        "db.connect() 退出时少了 commit（SQLAlchemy 的 engine.connect() 默认是回滚）"
    )
    with db.connect() as conn:
        conn.execute(f"DROP TABLE IF EXISTS {table}")


def test_pool_write_is_rolled_back_on_exception():
    import db

    table = "pool_rollback_probe"
    with db.connect() as conn:
        conn.execute(f"DROP TABLE IF EXISTS {table}")
        conn.execute(f"CREATE TABLE {table} (v TEXT)")

    with pytest.raises(RuntimeError):
        with db.connect() as conn:
            conn.execute(f"INSERT INTO {table} VALUES ('should-vanish')")
            raise RuntimeError("boom")

    with db.connect() as conn:
        row = conn.execute(f"SELECT count(*) FROM {table}").fetchone()
    assert row[0] == 0, "异常退出没有回滚，脏数据留下了"
    with db.connect() as conn:
        conn.execute(f"DROP TABLE IF EXISTS {table}")


# ---------------------------------------------------------------
# ② 并发下连接数有上限
# ---------------------------------------------------------------
def test_connection_count_is_capped_under_concurrency(_isolated_pool):
    import db

    monkeypatch = _isolated_pool
    monkeypatch.setattr(config, "PG_POOL_SIZE", 2)
    monkeypatch.setattr(config, "PG_MAX_OVERFLOW", 1)
    monkeypatch.setattr(config, "PG_POOL_TIMEOUT", 5)
    db.dispose()

    baseline = _activity_count()
    cap = config.PG_POOL_SIZE + config.PG_MAX_OVERFLOW
    peak = [baseline]
    stop = threading.Event()

    def sampler():
        while not stop.is_set():
            try:
                peak[0] = max(peak[0], _activity_count())
            except Exception:  # noqa: BLE001 —— 采样失败不影响断言主体
                pass
            time.sleep(0.02)

    t = threading.Thread(target=sampler, daemon=True)
    t.start()

    errors: list[BaseException] = []

    def worker():
        try:
            with db.connect() as conn:
                conn.execute("SELECT pg_sleep(0.3)").fetchone()
        except BaseException as exc:  # noqa: BLE001
            errors.append(exc)

    threads = [threading.Thread(target=worker) for _ in range(8)]
    for th in threads:
        th.start()
    for th in threads:
        th.join(timeout=30)

    stop.set()
    t.join(timeout=5)

    assert not errors, f"并发取连接出现异常：{errors}"
    assert peak[0] <= baseline + cap, (
        f"并发 8 个请求时连接数峰值 {peak[0]} 超过上限（基线 {baseline} + 池 {cap}）—— "
        "池没有生效，或者又被改回了每请求新建连接"
    )


# ---------------------------------------------------------------
# ③ 连接被掐断后能自愈
# ---------------------------------------------------------------
def test_pool_recovers_when_backend_is_killed(_isolated_pool):
    """杀掉池里唯一的连接，下一次取连接必须自愈。

    池要开成 1 条：默认池是 FIFO，多条连接时下一次取出的未必是被杀的那条，
    用例照样过，但证明不了 pre_ping 生效。
    """
    import db

    monkeypatch = _isolated_pool
    monkeypatch.setattr(config, "PG_POOL_SIZE", 1)
    monkeypatch.setattr(config, "PG_MAX_OVERFLOW", 0)
    monkeypatch.setattr(config, "PG_POOL_TIMEOUT", 5)
    db.dispose()

    with db.connect() as conn:
        pid = conn.execute("SELECT pg_backend_pid()").fetchone()[0]

    # 从库外掐掉这条已经归还池里的连接（模拟容器空闲断连 / 网络闪断）
    import psycopg

    with psycopg.connect(config.PG_DSN) as killer:
        killer.execute("SELECT pg_terminate_backend(%s)", (pid,))

    # pre_ping 应该在下次取出时发现这条已经死了，换一条新的
    with db.connect() as conn:
        new_pid = conn.execute("SELECT pg_backend_pid()").fetchone()[0]
        assert conn.execute("SELECT 1").fetchone()[0] == 1
    assert new_pid != pid, "拿到的还是被杀掉那条连接 —— pre_ping 没起作用"


# ---------------------------------------------------------------
# ④ 池打满 → PoolBusy → 503 + pool_busy
# ---------------------------------------------------------------
def test_pool_exhausted_raises_pool_busy(_isolated_pool):
    import db

    monkeypatch = _isolated_pool
    monkeypatch.setattr(config, "PG_POOL_SIZE", 1)
    monkeypatch.setattr(config, "PG_MAX_OVERFLOW", 0)
    monkeypatch.setattr(config, "PG_POOL_TIMEOUT", 1)
    db.dispose()

    held = threading.Event()
    released = threading.Event()

    def holder():
        with db.connect() as conn:
            conn.execute("SELECT 1")
            held.set()
            released.wait(timeout=10)

    th = threading.Thread(target=holder, daemon=True)
    th.start()
    assert held.wait(timeout=10), "持有连接的线程没起来"

    try:
        with pytest.raises(db.PoolBusy):
            with db.connect() as conn:
                conn.execute("SELECT 1")
    finally:
        released.set()
        th.join(timeout=10)


def test_pool_busy_maps_to_503(client, _isolated_pool):
    """走一次真实请求，不只是直接调处理器。

    为什么要真的打一次：`/chat` 链路里有好几处 `except Exception` 会把池满
    吞成"按无结果继续"（画像就是最危险的一处 —— 过敏排除名单会空着）。
    只测处理器证明不了这条路没被吞掉。
    """
    import db

    monkeypatch = _isolated_pool
    monkeypatch.setattr(config, "PG_POOL_SIZE", 1)
    monkeypatch.setattr(config, "PG_MAX_OVERFLOW", 0)
    monkeypatch.setattr(config, "PG_POOL_TIMEOUT", 1)
    db.dispose()

    held = threading.Event()
    released = threading.Event()

    def holder():
        with db.connect() as conn:
            conn.execute("SELECT 1")
            held.set()
            released.wait(timeout=15)

    th = threading.Thread(target=holder, daemon=True)
    th.start()
    assert held.wait(timeout=15), "持有连接的线程没起来"

    try:
        r = client.get("/kg/import/stats")
        assert r.status_code == 503, f"期望 503，实际 {r.status_code}：{r.text[:200]}"
        assert r.json()["detail"]["code"] == "pool_busy", r.text[:200]
    finally:
        released.set()
        th.join(timeout=15)


def test_profile_load_does_not_swallow_pool_busy(monkeypatch):
    """画像加载不能把"池满"吞成"没有画像"。

    吞掉的后果不是降级，是安全问题：过敏/忌口的排除名单变空，
    用户拿到一条没排除过敏原的建议，而接口返回 200。
    库真的不可用时才按"无画像"继续 —— 那是另一类故障。
    """
    import db

    import graph.nodes as nodes

    class _PoolBusyProfile:
        def __init__(self, *a, **k):
            pass

        def get(self, *_a, **_k):
            raise db.PoolBusy("测试")

    class _DeadDbProfile:
        def __init__(self, *a, **k):
            pass

        def get(self, *_a, **_k):
            raise RuntimeError("库真的挂了")

    state = {"user_id": "u-test"}
    monkeypatch.setattr("memory.long_term.UserProfile", _PoolBusyProfile)
    with pytest.raises(db.PoolBusy):
        nodes._load_profile(state)

    monkeypatch.setattr("memory.long_term.UserProfile", _DeadDbProfile)
    assert nodes._load_profile(state) == {}, "库不可用时应该按无画像继续"
