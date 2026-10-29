# 连接池代码评审

> 对照当前工作区里已换成 `db.connect()` 的实现。范围是 PostgreSQL 连接池，不含 needs_v4。
> 日期：2026-10-29。

## 1. 结论

可以合。事务语义是对的。

要改两处再依赖它承担线上流量：池打满时问答会把「没有画像」当成正常结果继续回答；新建物理连接没有连接超时。

## 2. 做对的部分

`db/engine.py` 避开了两件会静默丢数据的事。没有用 SQLAlchemy 的 `engine.connect()`（退出是回滚）。也没有对借出的连接写 `with conn:`（psycopg3 会把池里的连接真关掉）。业务侧都是 `with db.connect() as conn:`，块内不再自己 `commit`。`scripts/` 和 `api/health.py` 仍是短超时直连。

池参数和计划一致：`pool_size=10`、`max_overflow=5`、超时 3 秒、`pre_ping`、1800 秒回收。`deploy/systemd/nutri-graph.service` 的 `ExecStart` 写了 `--limit-concurrency 10`。`PoolBusy` 在 `app.py` 里映射成 503 和 `code=pool_busy`，响应体不带内部池参数。正常退出先 `commit()` 再归还；之后池默认的 rollback 作用在已经结束的事务上，已提交的写入不会被撤掉。

单次请求里的数据库占用是短的。画像、别名、会话都是用完即还，没有在等模型或写 Neo4j 时占着连接。`kg/promote.py` 的 `approve_bundle` 用 `FOR UPDATE` 只包住本事务里的 SQL，合并进图发生在连接归还之后。

## 3. 要改

### 3.1 池打满时，过敏过滤会空着继续回答

`PoolBusy` 是 `Exception`。`graph/nodes.py` 的 `_load_profile` 把所有异常记一条日志后返回 `{}`。`graph_query_node` 用这份空画像做过敏和忌口过滤，提示词里的排除名单同样是空的。这条请求最后是 200，不是 503。

别名缓存命中、本轮不再查库时，这个窗口是真实的：链接阶段不取连接，路由阶段取画像失败，图查询照跑。用户看到的是一条没有排除过敏原的食疗建议。`/chat` 上其它会逃出节点的取连接（会话归属、未收录登记、别名精确查询）仍然会变成 503。画像这条被单独吞掉了。

`_load_profile` 遇到 `db.PoolBusy` 应原样抛出，让现有的 503 处理器生效。库真的不可用时，维持现在的「记日志、按无画像继续」。那是另一类故障。

### 3.2 新建物理连接不受 `pool_timeout` 约束

`pool_timeout=3` 只限制池里没有空闲连接时等多久。池还没满、要新开一条到 PostgreSQL 的连接时，`create_engine` 没有 `connect_args` 的 `connect_timeout`。数据库不响应时，这条线程会一直堵到操作系统的 TCP 超时，远超过 3 秒。`/health` 自己用了 `connect_timeout=3`，池里的连接没有。补上同样的 3 秒，和池超时对齐。

## 4. 上线时要对齐的配置

`config.UVICORN_LIMIT_CONCURRENCY` 只有启动日志在读。真正卡住并发的是 systemd 里写死的 `--limit-concurrency 10`。`python app.py` 和 `uvicorn app:app` 都不会带这个参数，同步接口会进 anyio 默认约 40 个线程，池只有 15 条，多出来的请求会在 3 秒后 503。`.env` 里改 `PG_POOL_SIZE` 也不会改 systemd 的并发上限。两处数字要一起改，部署时要确认服务器上的 unit 已经是这份文件。

## 5. 其余

- `_engines` 按 DSN 缓存，但没有锁。两个线程同时第一次取连接会各建一个池，多出来的那个不会被 `dispose()` 关掉。进程内加一把锁即可。
- 连接在 `commit` / `rollback` 时已经断开的话，现在仍把这条连接还回池。默认 `pre_ping` 会在下次取出时丢掉它。回滚失败时先 `invalidate()` 再归还，不依赖 ping。
- `AliasStore`、`UserProfile`、`ShortTermMemory` 还留着 `dsn` 参数，实际连的是 `config.PG_DSN`。当前调用方传的就是这个 DSN，行为没变。之后若有人传入另一个库，会静默连错。
- `tests/test_db_pool.py` 的断连用例没有单独建池。池是先进先出时，下一次取出的不一定是刚被 `pg_terminate_backend` 杀掉的那条，用例仍可能通过，但不能证明 `pre_ping` 生效。503 那条是直接调用处理器，没有走一次真实请求。
