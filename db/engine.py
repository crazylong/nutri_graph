"""
db/engine.py —— PostgreSQL 连接池（SQLAlchemy 只用来做池，SQL 一行不改）

改这个文件前先看三条，前两条踩了都是"不报错但数据没了"：

1. **退出语义 = 提交**，必须和 `with psycopg.connect(...)` 完全一致。
   SQLAlchemy 的 `with engine.connect()` 退出是**回滚**。照那个写法，全站写操作
   会静默不落库，而且现有测试大多自己 commit，不一定能发现。
   所以这里 `raw_connection()` 拿到真正的 psycopg 连接，自己 commit / rollback / 归还。

2. **不要对池里的连接用 `with conn:`。**
   psycopg3 的 `Connection.__exit__` 在 commit 之后会 `close()` —— 只有连接带
   `_pool` 属性时才不关（那是 psycopg 自己的连接池才会设的）。
   SQLAlchemy 借出来的连接没有这个属性，`with conn:` 会把池里的连接真关掉。
   一律写 `with db.connect() as conn:`。

3. **`scripts/*` 保持 psycopg 直连**，不要"顺手统一"。
   一次性进程用池没有意义，`migrate` 还要 `autocommit=True`（建扩展、建索引）。
"""
from __future__ import annotations

import logging
import threading
from contextlib import contextmanager
from typing import Any, Iterator

import config

log = logging.getLogger("db.engine")

# 按 DSN 分别缓存。为什么不只留一个单例：config.PG_DSN 是可以在运行时被改的
# （测试用 monkeypatch 把它指到一个不可达的库，验证"库挂了不能把 /chat 带崩"）。
# 单单例会让这种改动静默失效 —— 旧代码每次 psycopg.connect(config.PG_DSN) 都重读。
_engines: dict[str, Any] = {}
_lock = threading.Lock()


class PoolBusy(Exception):
    """池在 `pool_timeout` 内拿不到连接。

    接口层把它映射成 503 + `code=pool_busy`。以前这个场景的行为是悬挂到超时后 500，
    前端拿不到任何可解释的信息。
    """


def _sa_url(dsn: str):
    """把 DSN 换成 SQLAlchemy 能用的 URL。

    用 make_url 解析而不是字符串拼接：远程 .env 里密码是 percent-encoding 的
    （`%21Jbgsn%402o26%26`），手拼会把 `%40` 当成字面量拼错。
    """
    from sqlalchemy.engine import make_url

    url = make_url(dsn)
    if url.drivername != "postgresql+psycopg":
        url = url.set(drivername="postgresql+psycopg")
    return url


def get_engine(dsn: str | None = None):
    """按 DSN 取池（惰性建）。没传就用 config.PG_DSN。

    惰性是因为测试里没有 FastAPI lifespan；按 DSN 缓存是因为 config.PG_DSN
    可能在运行时被改（见 _engines 的注释）。
    """
    key = dsn or config.PG_DSN
    with _lock:  # 两个线程同时首次取连接时会各建一个池，多出来的那个没人关
        eng = _engines.get(key)
        if eng is None:
            from sqlalchemy import create_engine

            url = _sa_url(key)
            eng = create_engine(
                url,
                pool_size=config.PG_POOL_SIZE,
                max_overflow=config.PG_MAX_OVERFLOW,
                pool_timeout=config.PG_POOL_TIMEOUT,
                pool_recycle=config.PG_POOL_RECYCLE,
                pool_pre_ping=config.PG_POOL_PRE_PING,
                # pool_timeout 只管"池里没空闲连接时等多久"。
                # 池还没满、要新开一条物理连接时，它管不到 ——
                # 数据库不响应的话这条线程会堵到 OS 的 TCP 超时（远不止 3 秒）。
                connect_args={"connect_timeout": config.PG_CONNECT_TIMEOUT},
            )
            _engines[key] = eng
            log.info(
                "PG 连接池已建立 host=%s db=%s pool_size=%s max_overflow=%s "
                "timeout=%ss connect_timeout=%ss recycle=%ss pre_ping=%s",
                url.host, url.database,
                config.PG_POOL_SIZE, config.PG_MAX_OVERFLOW, config.PG_POOL_TIMEOUT,
                config.PG_CONNECT_TIMEOUT, config.PG_POOL_RECYCLE, config.PG_POOL_PRE_PING,
            )
        return eng


def _invalidate(raw: Any) -> None:
    """把连接标成失效：归还时池会丢掉它，而不是留给下一个请求。"""
    try:
        raw.invalidate()
    except Exception:  # noqa: BLE001 —— 标记失败还有 pre_ping 兜底
        pass


def _is_pool_timeout(exc: BaseException) -> bool:
    try:
        from sqlalchemy.exc import TimeoutError as SATimeoutError  # noqa: N818
    except Exception:  # pragma: no cover —— sqlalchemy 缺失时不该到这一步
        return False
    return isinstance(exc, SATimeoutError)


@contextmanager
def connect(dsn: str | None = None) -> Iterator[Any]:
    """取一条连接。正常退出提交，异常回滚，最后归还池（不是断开）。

    与 `with psycopg.connect(config.PG_DSN) as conn:` 语义一致，只是连接来自池。
    """
    try:
        raw = get_engine(dsn).raw_connection()
    except Exception as exc:
        if _is_pool_timeout(exc):
            raise PoolBusy(
                f"数据库连接池已占满（pool_size={config.PG_POOL_SIZE} "
                f"max_overflow={config.PG_MAX_OVERFLOW} timeout={config.PG_POOL_TIMEOUT}s）"
            ) from exc
        raise

    conn = getattr(raw, "driver_connection", raw)
    try:
        try:
            yield conn
        except BaseException:
            # 块内出错：回滚。回滚本身失败说明这条连接已经断了 ——
            # 标成失效再归还，别把已知坏掉的连接留给下一个请求（不依赖 pre_ping）。
            try:
                conn.rollback()
            except Exception as exc:  # noqa: BLE001 —— 不能盖掉原始异常
                log.warning("回滚失败，连接标记为失效：%s", exc)
                _invalidate(raw)
            raise
        try:
            conn.commit()
        except Exception as exc:  # noqa: BLE001
            log.warning("提交失败，连接标记为失效：%s", exc)
            _invalidate(raw)
            raise
    finally:
        try:
            raw.close()  # 归还池，不是断开
        except Exception as exc:  # noqa: BLE001
            log.warning("归还连接失败：%s", exc)
            _invalidate(raw)


def ping() -> bool:
    """启动探活。失败只记日志，不抛 —— 池本身会在下次取连接时重试。"""
    try:
        with connect() as conn:
            conn.execute("SELECT 1").fetchone()
        return True
    except Exception as exc:  # noqa: BLE001
        log.warning("连接池启动探活失败：%s: %s", type(exc).__name__, exc)
        return False


def dispose() -> None:
    """关掉所有池。只在进程退出（lifespan shutdown）和测试里调。

    全部关掉而不是只关当前的：改了池参数之后必须让下一次取连接按新参数重建，
    只关一个会留下用旧参数建的池。
    """
    global _engines
    engines, _engines = _engines, {}
    for key, eng in engines.items():
        try:
            eng.dispose()
        except Exception as exc:  # noqa: BLE001
            log.warning("关闭连接池失败（%s）：%s", key.rsplit("@", 1)[-1], exc)
