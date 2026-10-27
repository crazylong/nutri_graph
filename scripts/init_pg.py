"""
scripts/init_pg.py —— 【已废弃，保留兼容入口】

原先这里是一长串 `CREATE TABLE IF NOT EXISTS`，问题是没有版本管理：
加列 / 改类型 / 建索引都无法追溯，一旦某条语句失败整批回滚，也说不清跑到哪一步。

现在 DDL 全部收编进 `migrations/`：
  002_business.sql      别名表 / 候选表 / 会话 / 画像（7 张，普通表）
  003_memory_vector.sql memory_fact（依赖 pgvector 扩展，失败不牵连上面那批）

本模块只保留为兼容入口 —— 老脚本、README、CI 里写的是 `python -m scripts.init_pg`，
让它等价于 `python -m scripts.migrate`，就不用去改那些调用点。

新代码请直接用：python -m scripts.migrate
"""
from __future__ import annotations

import sys


def init() -> int:
    from scripts import migrate

    print("提示：init_pg 已收编进 migrations，本次等价于 python -m scripts.migrate")
    return migrate.run()


if __name__ == "__main__":
    sys.exit(init())
