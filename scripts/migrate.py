"""
scripts/migrate.py —— 极简数据库迁移

为什么不在 init_pg.py 里继续堆 CREATE TABLE IF NOT EXISTS：
表数量已经翻倍，加列 / 改类型 / 建索引都没有版本管理，一旦线上表结构漂移就无法复现。

用法：
  python -m scripts.migrate            # 应用所有未执行的迁移
  python -m scripts.migrate --status   # 只看状态

约定：
  * 迁移文件放在 migrations/，命名 NNN_描述.sql，按文件名排序执行
  * 每个文件自带 BEGIN / COMMIT（用 autocommit 连接执行，不嵌套事务）
  * 执行成功后写 schema_migrations，重复执行无副作用
"""
from __future__ import annotations

import argparse
import hashlib
import sys
from pathlib import Path

import psycopg

import config

MIGRATIONS_DIR = Path(__file__).resolve().parent.parent / "migrations"

BOOTSTRAP = """
CREATE TABLE IF NOT EXISTS schema_migrations (
    version    TEXT PRIMARY KEY,
    checksum   TEXT NOT NULL DEFAULT '',
    applied_at TIMESTAMPTZ NOT NULL DEFAULT now()
)
"""


def _applied(conn) -> dict[str, str]:
    cur = conn.cursor()
    cur.execute("SELECT version, checksum FROM schema_migrations")
    return {v: c for v, c in cur.fetchall()}


def _files() -> list[tuple[str, Path]]:
    """按文件名排序返回迁移文件。

    过滤掉 `.` 开头的条目：macOS 打包/拷贝时会生成 `._xxx.sql` 这类 AppleDouble
    元数据文件（二进制），它以 .sql 结尾会被 glob 命中，读成 UTF-8 直接报
    UnicodeDecodeError，而且报错指向的行号毫无意义 —— 排查一次要花很久。
    """
    out = []
    for p in sorted(MIGRATIONS_DIR.glob("*.sql")):
        if p.name.startswith("."):
            continue
        out.append((p.name, p))
    return out


def status() -> int:
    with psycopg.connect(config.PG_DSN, autocommit=True) as conn:
        conn.execute(BOOTSTRAP)
        applied = _applied(conn)
    print(f"{'状态':<8} {'版本'}")
    for name, p in _files():
        mark = "已应用" if name in applied else "待执行"
        warn = ""
        if name in applied:
            digest = hashlib.sha256(p.read_bytes()).hexdigest()[:16]
            if applied[name] and applied[name] != digest:
                warn = "  ⚠️ 文件已变更但未新增迁移（生产环境禁止改已应用的迁移）"
        print(f"{mark:<8} {name}{warn}")
    return 0


def _hint(exc: Exception) -> None:
    """把最常见的两个环境问题翻译成人话，而不是只丢一个 psycopg 异常。"""
    msg = str(exc)
    if "vector" in msg and (
        "permission denied" in msg or "must be superuser" in msg
    ):
        print(
            "\n[需人工处理] pgvector 未标 trusted，业务角色装不了。请用超级用户执行一次：\n"
            "   psql -U postgres -d <你的库> -c 'CREATE EXTENSION IF NOT EXISTS vector'\n"
            "   然后重跑 python -m scripts.migrate（幂等，前面成功的迁移不会重做）",
            file=sys.stderr,
        )
    elif "vector" in msg and "is not available" in msg:
        print(
            "\n[需人工处理] 当前 PostgreSQL 镜像没有 pgvector。请换用 pgvector/pgvector:pg16：\n"
            "   docker-compose.yml 里 image 改为 pgvector/pgvector:pg16（PG16 同大版本，数据卷兼容）",
            file=sys.stderr,
        )
    elif "pg_trgm" in msg and "permission denied" in msg:
        print(
            "\n[需人工处理] pg_trgm 需要由库 owner 或超级用户创建："
            "\n   psql -U postgres -d <你的库> -c 'CREATE EXTENSION IF NOT EXISTS pg_trgm'",
            file=sys.stderr,
        )


def run() -> int:
    with psycopg.connect(config.PG_DSN, autocommit=True) as conn:
        conn.execute(BOOTSTRAP)
        applied = _applied(conn)
        todo = [(n, p) for n, p in _files() if n not in applied]
        if not todo:
            print("没有待执行的迁移，数据库已是最新。")
            return 0
        for name, path in todo:
            sql = path.read_text(encoding="utf-8")
            digest = hashlib.sha256(path.read_bytes()).hexdigest()[:16]
            try:
                conn.execute(sql)
            except Exception as exc:  # noqa: BLE001
                print(f"❌ {name} 执行失败：{exc}", file=sys.stderr)
                print("   该迁移已在自己的 BEGIN/COMMIT 内回滚，未记录版本。", file=sys.stderr)
                _hint(exc)
                return 1
            conn.execute(
                "INSERT INTO schema_migrations (version, checksum) VALUES (%s, %s) "
                "ON CONFLICT (version) DO UPDATE SET checksum = EXCLUDED.checksum",
                (name, digest),
            )
            print(f"✅ {name}")
    print("迁移完成。")
    return 0


def main() -> int:
    p = argparse.ArgumentParser()
    p.add_argument("--status", action="store_true", help="只查看状态，不执行")
    args = p.parse_args()
    return status() if args.status else run()


if __name__ == "__main__":
    raise SystemExit(main())
