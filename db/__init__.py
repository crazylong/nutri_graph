"""db —— 只有连接池，没有 ORM。SQL 保持原样写在各自模块里。

业务代码一律 `import db` + `with db.connect() as conn:`。
这里把 engine 的入口抬到包级，省得每个模块写 `from db.engine import connect`。
"""
from db.engine import PoolBusy, connect, dispose, get_engine, ping

__all__ = ["PoolBusy", "connect", "dispose", "get_engine", "ping"]
