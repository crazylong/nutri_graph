# plan_v9 评审（对照 needs_v9）· 第四轮

> 对照当前的 [docs/needs/needs_v9.md](../needs/needs_v9.md) 与 [docs/plan/plan_v9.md](../plan/plan_v9.md)。
> 上一轮的配额 SQL、限流区、父 span、打码两步、管理端页面、Langfuse 引用，都已写进施工正文。
> 未改代码。日期：2026-10-31。
> 施工以计划正文为准。§十三 是前几轮的复核记录，不代替 §四 到 §十。

## 1. 结论

可以按这一版开工。PERF-01 不在本版，Langfuse 整段删除，OBS-03 的表、记账出口、打码、详情页和 needs 正文已经对齐。

先改三处示例再写代码。这三处都是新写进去的片段，照抄会得到错的父节点、错的 IP，或一个不会自己跑起来的上报 timer。

1. **`span()` 若在进入时写行，父节点会指向自己。** 示例把「收尾写」和「进入时写」并列。进入时 `_current_span` 已经是这一行自己的 id。
2. **`/anon` 的日上限如果用连接对端，全站共用 `127.0.0.1`。** 计数在应用里，真实 IP 要用 `api/deps.py:37` 的 `client_ip()`。§十 的指标 timer 只写了 `OnUnitActiveSec=1min`，和 A2 的 `OnBootSec` 不一致，也和计划自己对抓取 timer 的结论相反：没有起点的 `OnUnitActiveSec` 不会第一次触发。
3. **needs 的 PERF-03 验收仍是「第二次 LLM 调用不增加」。** 计划按选项 A，第二次仍调模型。价格写成两个标量环境变量，表达不了按模型计价。

## 2. 上一轮落实

| 上一轮 | 正文现在 | 判定 |
|---|---|---|
| 配额 SQL 用 `CURRENT_DATE`，拒绝也加一 | `day` 由应用传入，不设默认值；`WHERE chat_usage_day.n < %s` 才更新，无返回行即超限 | ✅ |
| `60r/m` 改错文件 | 改 `deploy/nginx/00-nutri-graph.conf:17` 的 zone，location 只是引用 | ✅ |
| 父 span 没有来源；根的 kind 默认成 llm | `_current_span`；根写 `kind='node'`；费用查询全部 `WHERE kind = 'llm'` | ✅ 写法见 §3.1 |
| 打码晚于插入 | contextvar 管之后的插入，返回后按 `rid` UPDATE；`guard` 保留 output；普通健康问句不打码 | ✅ |
| 验收路径在图上不存在 | needs 改为「实际走过的路径」；本版不做 12 个节点 span | ⚠️ 验收第一句仍要求「与边一致」，见 §3.3 |
| 后台没有页面 | `Overview.vue`、`Trace.vue`、`Layout.vue`、`BundleDetail.vue`；`npm run build` 后整目录同步 `console/` | ✅ |
| Langfuse 摘除会断引用 | `config.py`、`.env.example`、`requirements.txt`、两处 monkeypatch、`test_sensitive.py` 都列了 | ✅ |
| `/anon` 没有上限 | 按 IP 每天 20 个，`anon_issue_day`，超限 `anon_limit_exceeded` | ⚠️ IP 的取法见 §3.2 |

抽查仍成立的锚点：`on_success` 在 `llm/middleware.py:158`，流式由 `llm/agent.py:139` 进入同一方法；`web_answer` 直接进入 `memory_write`（`graph/workflow.py:79`）；admin router 前缀已是 `/admin`（`api/admin.py:34`）；`client_ip()` 读 `X-Real-IP`，不用 `X-Forwarded-For`。

## 3. 开工前改的三处

### 3.1 `span()` 的写行时机

B1 的示例是：进入时 `_current_span.set(sid)`，`finally` 里先 `reset` 再写这一行，旁边又写「或按需在进入时写」。`record()` 用 `_current_span.get()` 当 `parent_span_id`。

进入时当前值已经是 `sid`。这时候写，父节点等于自己，树断一截。`reset` 之后当前值回到外层，那时写入，父节点才是包住它的那个 span。根的外层是空串，父节点为空，这是对的。

删掉「进入时写」。这一行只用进入前保存的 `parent`，放在 `reset` 之后。`with span(...)` 包住整个 `_invoke`，里面的 llm / tool 在块内调用 `record()`，读到的当前值才是根。

### 3.2 `/anon` 的 IP，以及指标 timer

`anon_issue_day` 在应用里计数。计划写「按 `$binary_remote_addr` / 真实 IP」。`$binary_remote_addr` 是 nginx 的变量，请求进到 Python 之后，连接对端是本机 nginx。用 `request.client.host` 时，所有人的键都是 `127.0.0.1`，每天 20 次领号会把整个站点的匿名入口关掉。

用 `client_ip()`（`api/deps.py:37-48`）。它取 nginx 写入的 `X-Real-IP`。没有这个头才退回连接对端，生产上不应走到这一支。

指标上报的 timer，A2 写的是 `OnBootSec=2min` 加 `OnUnitActiveSec=1min`。§十 的说明只剩 `OnUnitActiveSec=1min`。D1 已经写明：服务从未跑过时，`OnUnitActiveSec` 没有起点，`enable` 之后第一次不会来。两处写成一样：

```
[Timer]
OnBootSec=2min
OnUnitInactiveSec=1min
```

跑完再等 1 分钟，和抓取 timer 同一套语义。`AccuracySec` 默认 1 分钟，间隔也是 1 分钟时，上报可能晚一分钟。加上 `AccuracySec=5s`。

### 3.3 两份文档还差的两句

needs 第五节的验收仍是「同一句话第二次提问，LLM 调用计数不增加，p95 下降 > 60%」。计划 C2 按选项 A：第二次仍调模型，省掉的是 embedding 和 Neo4j。答案缓存继续不做，和需求第 3 条「看成本再定」一致。把 needs 那句验收改成选项 A 的口径，否则按需求验收这版过不了。

needs OBS-03 的验收第一句仍要求「父子关系与 `workflow.py` 的边一致」，注脚又说本版不做节点 span。没有节点 span 时，树上没有那些边。llm 和 tool 都挂在根 `node=chat` 下面。验收改成这句，和计划的 `tests/test_trace_span.py` 一致：根的 `kind='node'`，llm / tool 的 `parent_span_id` 指向它；走网页参考的那次有 `web_answer` 的 tool span，不要求出现 `assemble`。

`MODEL_PRICE_IN` / `MODEL_PRICE_OUT` 两个标量盖不住池里多个模型。`today_cost` 和按金额排的 Top 10 会偏。用一份按模型名的映射（输入价、输出价分开，单位写进注释）。映射里没有的模型记 0，并打一条 warning，避免静默少算。

路由挂在 `api/admin.py` 已有的 `prefix="/admin"` 上。函数路径写 `/trace` 和 `/kg/bundles/{bundle_id}/snapshot`。再写一遍 `/admin` 会变成 `/admin/admin/trace`。

## 4. 可以照做的

- 成功只在 `on_success` 记一笔。`pool_chain` 的账在 `kg/segment.py:55` 和 `graph/boundary.py:353`。`on_starved` 记 `ok=false`。
- 不建 `(ts::date)` 索引。今日和配额的日期都用上海时区，由应用传入。费用查询带 `kind = 'llm'`。缺表时统计字段为 null。
- 打码两步都做。范围只限 `_redact`。`guard` 保留 output。入参截断 2048 字。INSERT 失败不挡回答。`TRACE_ENABLED` 默认关：关时仍写 llm 的 token 行，不写 node / tool，入参留空。
- 图缓存 key 用忌口的排序哈希，只缓存单次 `_query.run`。epoch 加在 `merge_pending`、`rollback` 和真正写图的回填上。`--dry-run` 不增加 epoch。embedding 只包 `rag/embed.py`。Redis 失败当未命中。
- SSE 在 `work()` 的 `finally` 里释放信号量。后台池用有界队列。`/metrics` 增加 nginx 的精确 location 并限制来源。探活缓存是未到期用上一次结果。
- 抓取 timer 用 `OnBootSec` 和 `OnUnitInactiveSec`。日志分开。快照 sha256 用排序后的 JSON。管理端三个页面 build 后整目录覆盖 `console/`。
- 不引入 Prometheus 客户端。本版不加 worker。Langfuse 的模块、配置、依赖和测试引用按 §八 的表删。
- 测试在服务器上跑，用 `nutri_graph_test`，不对生产图开 `ALLOW_GRAPH_TESTS`。
