# plan_v9 —— 成本 / 性能 / 可观测 / 图谱治理 九项施工计划

> 生成时间：2026-10-31 · 基线 commit `6116bd0 (v8)`
> 上游需求：[`docs/needs/needs_v9.md`](../needs/needs_v9.md)（现状与验收口径），问题源流 [`docs/brain/brain_v1.md`](../brain/brain_v1.md)
> 已评审：[`docs/review/review_v9.md`](../review/review_v9.md) —— **四轮复核结论与逐条实测见 §十三**
> （一、二、三轮共 10 条问题全部成立并已修入正文；**第四轮结论：可以按本版开工**，仅 3 处示例片段需先改，已改）。
> 性质：**只写怎么施工**。每一条都标了要改的 `文件:行`，可复核。
> 施工原则：**每个 Phase 独立可上线、独立可回滚**；新增能力一律先加总开关（默认关闭），
> 这样出问题不用回滚代码，改配置重启即可。

---

## 一、范围与结论

| 编号 | Phase | 大致人日 |
|---|---|---|
| OBS-01 /metrics + 告警 + `/health` 补 Redis | A | 2.5 |
| OBS-02 请求标识（含后台线程传递） | A | （含上） |
| COST-01 LLM 调用明细落库 + 后台视图 | B | 1.5 |
| OBS-03 全链路 span 埋点 + 后台 trace 详情页 | B | 1.5 |
| COST-02 `/chat` 用户级配额 | B | 1.0 |
| PERF-02 SSE 与后台线程并发收敛 | C | 2.0 |
| PERF-03 embedding / 检索结果缓存 | C | 1.5 |
| KGD-03 补录队列定时任务 | D | 1.0 |
| KGD-02 来源快照 | D | 1.5 |
| **合计（不含验证）** | | **≈ 12.0 人日** |
| 端到端验证与部署演练 | | 1.0 |

**施工顺序：A → B → C → D。** 两条硬依赖决定这个顺序，见 §三。
例外：`KGD-03`（定时任务）本身零依赖、半天就能上，**如果急着让补录闭环自动跑起来，可以从 D 里抽出来提前做**。

---

## 二、九项 × 现状锚点速查

| 编号 | 要动的地方 | 关键点 |
|---|---|---|
| OBS-01 | 新增 `api/metrics.py`；改 `api/health.py:57-73`；改 `app.py:117-121` 挂 router | 无 Prometheus 依赖，手写文本格式 |
| OBS-02 | 新增中间件；改 `utils/logging_config.py:110-113` 的 formatter；改 `api/chat.py:123-160` 与 `graph/nodes.py:738-760` 的线程创建 | **后台线程不会自动继承 contextvars** |
| COST-01 | 改 `llm/middleware.py:145-162`、`llm/agent.py:128-139`；新增迁移 026；改 `api/admin.py:64-129` 的 `/admin/stats` | 中间件拿不到 user_id，必须靠 OBS-02 的 contextvars |
| OBS-03 | 复用 026 的 `llm_call_log`（span 字段）；`middleware.py` 写 llm span；`web_answer_node`/`link_node`/`graph_query_node` 写 tool span；`/admin` 加 trace 详情页 | 一张表两用：账本 + span 树；kind ∈ llm/node/tool |
| COST-02 | 新增 `security/quota.py`；改 `api/chat.py:58-80` `_prepare` | 命名沿用 `Decision.code` 风格 |
| PERF-02 | 改 `api/chat.py:123-160`、`graph/nodes.py:738-760`、`chatbus.py:108-124` | `chatbus.blpop` 每次建连 |
| PERF-03 | 新增 `utils/cache.py`；改 `graph/nodes.py:147-161`、`rag/embed.py`、由 `kg/merge.py` 触发失效 | key 必须含「图版本」 |
| KGD-03 | 新增 `deploy/systemd/nutri-graph-fetch.{service,timer}`；改 `scripts/run_fetch.py:50-53` 的日志初始化 | 别再 `basicConfig` |
| KGD-02 | 新增 `kg/snapshot.py`；改 `kg/fetch.py:183-204` `_persist`；新增迁移 027 | 先落本地磁盘，对象存储后置 |

---

## 三、两条硬依赖

```
        ┌── OBS-02 contextvars ──┬──► COST-01 能给支出打上 user/session/purpose
        │                        └──► 后台线程也能被串到同一次请求
A ──────┤
        └── OBS-01 /metrics ─────────► B/C/D 的效果才量化得出来（缓存命中率、队列深度、QPS）
```

- **没有 OBS-02 就没有真正可用的 COST-01**：`llm/middleware.py` 那一层完全不知道这次调用属于谁。
- **没有 OBS-01 就无法验收 PERF-03 / KGD-03**：「命中率提升了多少」和「队列在不在流动」没有数字就是空话。

---

## 四、Phase A：可观测底座

### A1 · `/metrics` 端点（不引入新依赖）

**新增 `api/metrics.py`**，手写 Prometheus 文本格式，30 秒内对所有重型取值做缓存（避免监控抓取本身变成负载）：

```python
@router.get("/metrics")
def metrics(): ...
```

| 指标 | 取值来源 | 说明 |
|---|---|---|
| `nutri_chat_total{status}` | 进程内计数器，在 `api/chat.py:112` 与 `:163` 入口各 +1 | status ∈ ok / degraded / abstain / error |
| `nutri_chat_latency_ms` | 用 `api/chat.py:87-100` 已有的 `elapsed_ms` 做滑动窗口 | 暴露 avg / p95 |
| `nutri_graph_hit_ratio` | 上面两个派生 | 告警依赖它 |
| `nutri_degraded_ratio` | `result["_degraded"]` | |
| `nutri_abstain_ratio` | 弃答比例（见下方修正） | 告警依赖它 |
| `nutri_llm_pool_available` | `llm.pool_snapshot()`（`/llm/pool` 已在用，`api/system.py:26-28`） | 0 = 全池不可用 |
| `nutri_sms_sent_today` | `SELECT count(*) FROM sms_send_log WHERE ok AND created_at > ...`（与 `security/ratelimit.py:113-117` 同一条 SQL） | 带 60s 缓存 |
| `nutri_fetch_queue_depth` | `SELECT count(*) FROM kg_fetch_task WHERE status IN ('queued','running')` | 给 KGD-03 用 |

⚠️ **不要**为了这个新装 `prometheus_client`，这个体量手写 40 行就够了，少一个依赖、少一处版本兼容风险（项目已经吃过 `ragas × langchain-community` 的亏）。

⚠️ **`nutri_abstain_ratio` 定义修正**（review_v9 §3.5）：不要定义成「`_graph_hit` 为 false 的比例」。图未命中时仍可能走网页参考或向量降级并给出回答；弃答是图和向量都没有的那一支（`graph/nodes.py:513-530`）。按「未命中图」算，这个比率就是 `graph_hit_ratio` 的反面，告警文案「掉下来＝系统在编答案」对不上。

⚠️ **`/metrics` 出不了 nginx**（review_v9 §3.5）：nginx 只有 `location = /health` 精确反代，`/metrics` 会落到 `location /` 的 `try_files`，返回 `index.html` 且永远是 200。**必须在 `deploy/nginx/nutri-graph-locations.conf` 加 `location = /metrics` 反代到 8810**，并加 `limit_req` + 限来源（内网或 token），不要做成公开页（它会露出池状态和短信量）。

配置：`METRICS_ENABLED=true`、`METRICS_CACHE_TTL_S=30`。

### A2 · 告警（阿里云云监控，免费额度够）

| 规则 | 条件 | 为什么是它 |
|---|---|---|
| 服务不可用 | `/health` 连续 3 次非 200 | 基础（站点监控可直接做） |
| 图谱失灵 | `graph_hit_ratio` 1h 均值 < 0.3 | 通常意味着实体链接或图数据坏了，用户已经在拿降级答案 |
| 模型池耗尽 | `llm_pool_available = 0` 持续 5 分钟 | 全站开始回「暂时答不了」 |
| 短信异常 | `sms_sent_today` 日环比 > 5x | 被刷的第一信号 |
| 补录停滞 | `fetch_queue_depth > 0` 且连续 N 个采集周期不变 | N 按 10 分钟抓取周期定（needs 验收要求 FDC key 失效后告警，见 review_v9 §3.5） |

⚠️ **告警通路必须写成命令 + timer，不能只写一句「定时上报」**（review_v9 §3.5 + 第三轮 review §5）：
阿里云云监控**不会解析 Prometheus 文本**。`graph_hit_ratio`、`llm_pool_available`、短信环比、`fetch_queue_depth`
是「指标值」，只有「`/health` 非 200」能靠站点监控做。具体落地：

1. 新增 `scripts/report_metrics.py`：读本机 `http://127.0.0.1:8810/metrics`（或直接调 `api.metrics` 的取数函数），
   转成阿里云「自定义监控」的 `MetricList`（`PutCustomMetric`）上报，带 `MetricName` + `Dimensions` + `Value`。
2. 新增 `deploy/systemd/nutri-graph-metrics.timer`：

   ```
   [Timer]
   OnBootSec=2min
   OnUnitInactiveSec=1min
   AccuracySec=5s
   ```

   配套 `nutri-graph-metrics.service`（`Type=oneshot`），日志进 `logs/metrics.log`（不要和 `backend.log` 共用）。
   ⚠️ **必须带 `OnBootSec`**：`OnUnitActiveSec` 相对「上一次激活」计时，服务从未跑过时没有起点，
   `enable` 之后第一次不会来（与 D1 的抓取 timer 同一个坑，第四轮 review §3.2 指出两处必须一致）。
   用 `OnUnitInactiveSec` 而不是 `OnUnitActiveSec`：每次**跑完**再等 1 分钟，语义与抓取 timer 相同。
   `AccuracySec` 默认就是 1 分钟，间隔 1 分钟时上报可能整体晚一分钟，显式写 `5s`。
3. 告警规则配在云监控控制台，引用上面的自定义指标名。

没有这一步，needs 验收里「2 分钟内收到告警」只有 Redis 宕机这一条能成立。

**D1 里的 `backfill_nutrients --dry-run` 干跑不会自己变告警**（review_v9 §3.5）：要么增加 `nutri_nutrient_drift` 指标并上报，要么从本版拿掉这条告警依赖。

### A3 · `/health` 补 Redis + 探活保护

改 `api/health.py:57-73`，在 `checks` 里加一项 Redis（端口/密码来自 `config.REDIS_*`，`ping()` 即可）。
同文件加一个 **10 秒 TTL 缓存**：现在每次探活会新建一个 Neo4j driver（`health.py:22-29`）和一次 Milvus connect（`:35-44`），负载均衡高频探活时会放大成负担。
**措辞修正**（review_v9 §3.5）：缓存语义是「未到期用上一次结果，到期重新探测」，不是「到期即从缓存取」（后者会被读成永远不重新探测）。10 秒窗口落在「2 分钟内告警」里，可保留。

### A4 · 请求标识（这一步有两个必踩的坑）

改三处：

1. **新增中间件**：生成短 `rid`（uuid4 前 8 位），写入 `contextvars`。
   ⚠️ **命名不要用 `request_id`** —— 仓库里 `request_id` 已经是「未收录食材登记编号」（`kg/ingredient_request.id`，见 `kg/request.py:94`、`scripts/clean_request_noise.py:53`），同名会让人看错日志。
   ⚠️ **必须用纯 ASGI 中间件，不要用 `@app.middleware("http")`**（review_v9 §4.2）：Starlette 的 `BaseHTTPMiddleware` 文档标明 contextvars 不可靠。`rid` 在进路由之前写入；`user_id`/`session_id` 不要在中间件里猜（`Depends` 还没跑），由 `_prepare` 注入（计划原稿这句对，保留）。
2. **日志注入**：在 `utils/logging_config.py:16-32` 加一个 `Filter`，给每条 record 挂 `rid`；formatter（`:110-113`）加 `%(rid)s`。
   ⚠️ **Filter 必须给缺省 `rid` 写 `-`**（review_v9 §4.2）：formatter 一旦加 `%(rid)s`，没挂上 Filter 的记录会在格式化时抛错、这条日志被吞掉；uvicorn 访问日志也走 root handler。`configure_logging()` 的 `%` 格式串是启动时固定的，加字段要同步改 formatter。
3. **响应头 + SSE**：响应头回传 `X-Trace-Id`；SSE 每个事件带 `rid`——**加在 `chatbus._frame` 上**，不能只加在 `Live.emit`（读侧自己拼的 `error`/`done`，`chatbus.py:167` 附近，不带）；`rid` 同时作为 OBS-03 span 表的 `trace_id`，实现日志 ↔ span 表互跳（不再依赖 Langfuse）。

**坑（必须处理）**：`threading.Thread` **不会自动继承 contextvars**（asyncio 的 task 会，thread 不会）。而本项目恰恰有四处后台线程：

- `api/chat.py:158` SSE 工作线程
- `graph/nodes.py:738-744` 画像抽取
- `graph/nodes.py:747-760` 会话摘要
- `kg/segment.py:75` 食材识别超时包装

不做处理的话，request-id 在**最需要它的地方**（后台线程写的日志）集体失效，而且 COST-01 的用户归因也跟着失效。
统一收口到 `utils/spawn.py`：

```python
def spawn(name, target, *args):
    ctx = contextvars.copy_context()
    threading.Thread(target=lambda: ctx.run(target, *args), name=name, daemon=True).start()
```

四处线程创建全部改成它（顺带解决了未来新增线程重复踩坑的问题）。

⚠️ **`purpose` 也要靠 contextvars 传，且要在线程函数内、调模型前设置**（review_v9 §4.2）：`spawn()` 会把父线程**当时的值**拷进去。若父线程正处在 `purpose("answer")` 里，画像线程不改写就会把画像记成主回答。后台线程进入后要显式 `purpose("profile")`/`purpose("summary")` 覆盖。

**验收**：一次 `/chat` 的日志里，三个线程的 `rid` 相同；`grep rid` 能拿到完整顺序；能在 OBS-03 的 span 表里定位同一次请求的整棵 span 树。

---

## 五、Phase B：成本账本

### B1 · COST-01 + OBS-03：LLM 调用明细落库（兼全链路 span 表）

> **本表一张表两用**：既是 COST-01 的账本，也是 OBS-03 的 span 表（`parent_span_id` + `kind` + `node` 字段）。
> 埋点一次写成，避免「先建纯账本表、OBS-03 再回来改 schema」的返工。
>
> ⚠️ **表名统一用 `llm_call_log`，本版不改名**（第三轮 review §4.1）：需求正文写的就是这个名字，
> 改名要同步回写 needs 并全文替换，容易造成双口径。名字虽不完美（它也存 node/tool span），
> 但**代价是每条查询必须带 `kind='llm'`** —— 这一条已写成硬约束（见下方「费用统计必须强制过滤」），
> 不依赖改名来兜底。

**迁移 `026_llm_usage.sql`**（迁移器用法见 `scripts/migrate.py`；本表是纯业务表，不涉及 pgvector，不需要超级用户前置操作）：

```sql
CREATE TABLE IF NOT EXISTS llm_call_log (
    id                BIGSERIAL PRIMARY KEY,
    ts                TIMESTAMPTZ NOT NULL DEFAULT now(),
    rid               TEXT        NOT NULL DEFAULT '',   -- 对应 request-id（OBS-02）
    span_id           TEXT        NOT NULL DEFAULT '',   -- 本 span 唯一 id
    parent_span_id    TEXT        NOT NULL DEFAULT '',   -- 空 = 根节点（整次请求）
    kind              TEXT        NOT NULL DEFAULT 'llm',-- llm | node | tool
    node              TEXT        NOT NULL DEFAULT '',   -- guard/link/graph_query/web_answer/generate...
    user_id           TEXT        NOT NULL DEFAULT '',
    session_id        TEXT,
    model             TEXT        NOT NULL DEFAULT '',
    purpose           TEXT        NOT NULL DEFAULT 'unknown',
    input             TEXT        NOT NULL DEFAULT '',   -- prompt / 搜索词 / cid（**截断 ≤2KB**）
    output            TEXT        NOT NULL DEFAULT '',   -- 回答 / 命中数（**截断 ≤2KB**）
    prompt_tokens     INT         NOT NULL DEFAULT 0,
    completion_tokens INT         NOT NULL DEFAULT 0,
    latency_ms        INT         NOT NULL DEFAULT 0,
    ok                BOOLEAN     NOT NULL DEFAULT true
);
CREATE INDEX IF NOT EXISTS idx_llm_call_ts        ON llm_call_log (ts DESC);
CREATE INDEX IF NOT EXISTS idx_llm_call_user_ts   ON llm_call_log (user_id, ts DESC);
CREATE INDEX IF NOT EXISTS idx_llm_call_rid       ON llm_call_log (rid);
CREATE INDEX IF NOT EXISTS idx_llm_call_session   ON llm_call_log (session_id, ts DESC);
```

> ⚠️ **不要建 `(ts::date)` 表达式索引**（review_v9 §3.1）：`timestamptz→date` 依赖会话时区，PG 标为 STABLE，表达式索引要求 IMMUTABLE，这条会让 `scripts.migrate` 在 026 失败。「今日」直接按 `ts` 范围查（上海日历日），`idx_llm_call_ts` 已够。

**记账出口只有一个（review_v9 §3.1 关键修正）**：

- **成功路径只记一笔**：`llm/agent.py:139` 的 `plan.on_success(...)` 就是 `llm/middleware.py:158` 的那个方法，流式和 `invoke_model` 都走这里。**不要在表格里把「middleware」和「agent 流式」两处都当埋点**，否则每条流式回答记两笔。成功记账统一挂在 `on_success` 一处。
- **`pool_chain` 记账必须写在调用点，不能写在 `pool_chain()` 里面**（第三轮 review §4.3）：
  `llm/router.py:184` 的 `pool_chain()` **只返回链对象**，函数体内看不到 token、也不知道这次调用花了多少。
  所以账记在真正 `invoke` 的地方：`kg/segment.py:55`（食材识别）与 `graph/boundary.py:353`（画像抽取）
  的 `resp = chain.invoke(...)` 旁边。这两处不经过 `on_success`、也不调 `mark_success`，
  只改 `on_success` 的话这两笔成本永远不进账。
- **失败与空正文重试也记**：`on_error` 现在不落账；**同步路径的空正文重试走 `on_starved`**
  （`llm/middleware.py:209` 与 `:238`，第三次轮 review §4.3 补），这次调用**有 token**，
  不记则账单少一笔；流式同理（`llm/agent.py:118-126`，`on_starved` 在 `:141`）。
  统一用表上的 `ok` 记 `ok=false`，不要只在成功时插入。
- **换模型的两次尝试**记在 `on_error` 上（覆盖额度用尽后的切换），token 各记各的。

**埋点位置汇总**（token 由 `_usage()` 已算好，直接复用）：

| 调用场景 | 位置 | purpose | kind |
|---|---|---|---|
| 回答生成（同步+流式，唯一出口） | `llm/middleware.py:158` `on_success` | `answer` | `llm` |
| 食材识别（`pool_chain` 的 invoke 处） | `kg/segment.py:55` | `segment` | `llm` |
| 画像抽取（`pool_chain` 的 invoke 处） | `graph/boundary.py:353` | `profile` | `llm` |
| 同步空正文重试（有 token） | `llm/middleware.py:209` / `:238` 的 `on_starved` | 同调用方 | `llm`（`ok=false`） |
| 会话摘要 | `graph/nodes.py:747-758`（走 `invoke_model`→`on_success`） | `summary` | `llm` |
| 抓取时食材画像 | `kg/draft.py:171-173`（`invoke_model`→`on_success`） | `draft` | `llm` |
| 评估 | `eval/run.py:43` `primary_model()`（不进中间件） | `eval` | `llm` |
| `--probe` | `llm/router.py:250` 直接 `mark_success` | `probe` | `llm` |

实现方式是 `llm/usage.py` 提供 `purpose("answer")` 上下文管理器 + `record(...)`，
靠 **A4 的 contextvars** 取 `rid / user_id / session_id`（由 `api/chat.py:58-80` `_prepare` 注入）。
**兜底**：没有包裹到的调用用 `unknown` 落库，而不是静默丢弃 —— 宁可有 `unknown` 这一行提醒你漏了埋点，也不能让账本缺一块。

**OBS-03 的 span 埋点（与上面同一张表，kind 区分）**：

⚠️ **必须先补「当前 span」的 contextvar，否则树连不起来**（第三轮 review §4.1，这是本版最关键的修正）：
`record()` 只从 contextvars 取 `rid/user_id/session_id`，`parent_span_id` 会是默认空串 → 根、llm、tool 全是根，
`tests/test_trace_span.py` 要求的父子关系不成立，后台也无法「逐层展开」。做法：

```python
_current_span: ContextVar[str] = ContextVar("current_span", default="")

@contextmanager
def span(kind, node, ...):
    sid = uuid4().hex[:16]
    parent = _current_span.get()          # ← 进入前保存，这就是父节点
    token = _current_span.set(sid)
    try:
        yield sid
    finally:
        _current_span.reset(token)        # 回到外层
        _write(sid, parent, kind, node, ...)   # ← 只在 reset 之后写这一行
```

⚠️ **写行的时机是硬约束，不是风格问题**（第四轮 review §3.1）：**只能在 `reset` 之后写，用进入前保存的 `parent`**。
- **若在进入时写**：那时 `_current_span` 已经等于 `sid`，`record()` 读到的当前值就是自己 → `parent_span_id == span_id`，
  树断一截（自己指向自己）。
- **`reset` 之后**当前值回到外层，这时写入，父节点才是包住它的那个 span。
- **根的外层是空串** → 根的 `parent_span_id=''`，这是对的。

`with span("node", "chat")` 包住整个 `_invoke`，里面的 llm / tool 在块内调 `record()`，读到的当前值才是根。
`spawn()` 拷贝 context 时，画像/摘要线程的 llm 行才能挂到同一次请求的根上。

- **根 span**：在 `api/chat.py:85` `_invoke()` 里写根节点，**`node='chat'`、`kind='node'`**。
  ⚠️ **必须显式写 `kind='node'`**：列默认值是 `llm`（见上方 SQL `DEFAULT 'llm'`），
  不写的话每次提问会在账上多出一行 0 token 的「模型调用」，`today_calls` 直接虚高。
  这一步**替代原来的 `trace(...)`**（`observability/tracing.py` 的 Langfuse 依赖本版摘除）。
- `llm` span：上面的 LLM 记账本身就是 llm span，`input`=prompt、`output`=回答、token、模型、耗时天然齐全。
- `tool` span：工具型节点里加：
  - `web_answer_node`（网页搜索）：`input`=搜索词、`output`=返回条数/URL。
  - `link_node`/`graph_query_node`（图查询）：`input`=cid/实体、`output`=命中数。
  - 这些是「往节点函数里加几行」，收口点已存在，不重铺调用链。
- **节点 span 本版不做**（第三轮 review §4.3）：需求正文第 2、3 条只要求 llm span + 三个 tool span，
  没有要求 12 个节点各记一笔。若后续要每步耗时，在 `build_workflow()` 注册节点时统一包一层，
  不要每个节点函数手写。

**费用统计必须强制过滤 `kind='llm'`**（第三轮 review §4.1，不是可选项）：
`today_calls` / `today_tokens` / `today_cost` / `top_users` 四个查询**全部**要带 `WHERE kind = 'llm'`，
否则根 span 和 tool span 会混进「调用次数」与「费用」，数字对不上账单。这条**与表名是否改成 `trace_span` 无关**。

**表名**：需求正文写的是 `llm_call_log`。若采纳改名 `trace_span`，**必须回写 needs 并全文只用一个名字**；
不要出现「B1 用旧名、§13 又提改名」的双口径。本计划**统一用 `llm_call_log`**，改名作为可选后续。

- **后台 trace 详情页（OBS-03）**：见下方「后台页面施工（admin-web）」——**不是只加一个 JSON 接口**。

**OBS-03 的硬约束（第三轮 review §4.1 / §4.2，不可省）**：

1. **敏感信息打码要做两步，只做一步都会漏**（第三轮 review §4.2）：

   时序问题：政治类在 `sensitive_guard_node`（`graph/nodes.py:59-60`）就带 `_redact`，这条路径不调模型；
   但输出扫描与红旗兜底发生在**模型返回之后**，而 `on_success` 插入时回答原文已经进表。
   `api/chat.py:101-108` 的 `_redact` 判定在 `_invoke` **整段结束之后** —— 插的时候还看不到要不要打码。

   - **第一步（管未来）**：`_redact` 一置位就写进 contextvar。之后的 `record()`（含**已经启动的画像线程**）
     直接写 `kg/sensitive.py:63` 的 `REDACT`，不再写原文。
   - **第二步（管过去）**：`_invoke` 返回后若 `_redact` 为真，按该 `rid` 把**已插入**的 `input`/`output`
     `UPDATE` 成占位符。`_redacted_kind == "guard"` 时**保留 output**（就医回答要留下，`graph/nodes.py:815-816`），
     与现在 `redact_trace` 的 `keep_output` 语义一致。

   ⚠️ 只在插入时看标志 → 盖不住已写下的那一行；只在返回后 UPDATE → 盖不住画像线程随后又插的一行。
   ⚠️ **打码范围只用现有 `_redact`，不要扩成「所有健康问句」**：普通健康问句今天本来就进 `message` 表，
   全打码后详情页入参为空，验收「每步有入参」反而做不到。
2. **截断 + 吞异常**：`input`/`output` 写入前统一截断到 **2048 字**；INSERT 必须 `try/except` 吞掉，
   **绝不能因为写 span 失败影响回答**（与账本同一条纪律）。第一版不做批量缓冲。
3. **写放大预期**：COST-01 是「每请求 1 笔」，OBS-03 变成「每 LLM + 每工具各 1 笔」（一问 3~5 笔）。
   上线后看 `pg_stat_user_tables.n_tup_ins`，确认 INSERT 没成为热路径瓶颈。
4. **`TRACE_ENABLED` 的语义要重写**（第三轮 review §4.1）：一次 INSERT 同时写 token 和入参，不能简单「停 span、账本照记」。
   正确语义：**关掉时 `kind='llm'` 的 token 行照写（`input`/`output` 留空），`kind='node'/'tool'` 行不写**。
   **默认值改成 `false`** —— 入参里有问句和健康自述，符合文首「新能力默认关闭」。

**后台页面施工（admin-web 是 Vue 应用，不是「加个 JSON 接口」）** —— 第三轮 review §4.4：

| 要看见的 | 现状 | 要改的文件 |
|---|---|---|
| 今日费用 / 次数 / 按金额的 Top 10 | `Overview.vue` 只画活跃用户、待审补录、未收录状态 | `admin-web/src/views/Overview.vue` 加卡片，取 `/admin/stats` 新字段 |
| 按 `rid` 展开的 span 树、按 session 先列轮次 | `router.js` 里没有这个页面 | 新增 `admin-web/src/views/Trace.vue` + `router.js` 路由 + `views/Layout.vue` 菜单项 |
| 快照一键打开（D2 也缺） | `views/BundleDetail.vue` 没有入口 | `BundleDetail.vue` 加按钮 → 调 `GET /admin/kg/bundles/{id}/snapshot` |

- **后端接口**（⚠️ **装饰器里不要再写 `/admin`**，`api/admin.py:34` 的 `APIRouter(prefix="/admin")` 已经带了前缀，
  再写一遍会变成 `/admin/admin/trace` —— 第四轮 review §3.3）：

  | 公开 URL | 装饰器路径 | 用途 |
  |---|---|---|
  | `GET /admin/trace?rid=` | `@router.get("/trace")` | 返回该 `rid` 的 span 树 |
  | `GET /admin/trace/sessions?session_id=` | `@router.get("/trace/sessions")` | 只返回轮次摘要 |
  | `GET /admin/kg/bundles/{bundle_id}/snapshot` | `@router.get("/kg/bundles/{bundle_id}/snapshot")` | 读 `snapshot_uri`、核对 sha256、返回 JSON |

  权限沿用现有 admin 判定（`router` 上的 `dependencies=[Depends(require_admin)]`）。
- ⚠️ **部署必须整目录覆盖**：`npm run build` 后把 `admin-web/dist` **整个目录**同步到
  `/var/www/nutri_graph/console/`。只传 `index.html` 会留下旧的带 hash 的 chunk，页面加载 404。
  `console/index.html` 已是 `no-store`，不需要 bump `?v=`。
- 这一步要写进 §十 的部署步骤（现在没有）。

**与 `--reset` 解耦**（现在 `scripts/check_llm_pool.py --reset` 会把 token 计数一起清掉）：
把 `--reset` 的语义收缩成「只清模型可用性状态」，token 累计就是 `llm/pool.py:342-351` 的 `_states` 重建 + 删文件动作 ——
把 `calls / prompt_tokens / completion_tokens` 改成「reset 时保留」即可（几行改动），另外日常用量以 `llm_call_log` 为准。JSON 里的累计可以保留也可以不再当账，**两套数不要同时对外当「今日费用」**。

**后台视图**：`/admin/stats`（`api/admin.py:64-129`）增加字段：`today_calls`、`today_tokens`、`today_cost`、`top_users`。
- **模型价格必须是「按模型名的映射」，不能是两个标量**（第四轮 review §3.3）：池里有多个模型（`LLM_MODEL_POOL`），
  单价各不相同；`MODEL_PRICE_IN` / `MODEL_PRICE_OUT` 这种单值会把所有模型按同一个价算，`today_cost` 与按金额排的
  `top_users` 都会偏。
  做法：配置里放一份映射，键是模型名（与 `llm/pool.py` 的 `spec.name` 一致），值是 `{in, out}`：

  ```yaml
  # 单位：元 / 千 token（写进注释，避免 1k / 1M 混淆）
  LLM_PRICE_MAP:
    qwen-max:        { in: 0.02, out: 0.06 }
    qwen-plus:       { in: 0.0008, out: 0.002 }
    # ...
  ```

  ⚠️ **映射里没有的模型记 0 并打一条 `warning`**（带模型名），不要静默少算 —— 否则新加模型后费用会悄悄偏低，
  而「与账单误差 < 10%」的验收发现不了是哪一笔漏了。
  `today_cost` 按标价算；`top_users` 按估算金额排（不是按 token 排）—— review_v9 §3.1：按 token 和按钱排不是同一份名单。
- **`/admin/stats` 缺表时字段留 null，不整页 503**（`api/admin.py:84-87` 的 `pending=None` 是既有写法；503 是列表接口 `:136-140` 的写法）。`llm_call_log` 未迁移时若让整页 503，现有运营概览会挂。
- 「今日」按**上海时区**切（`api/admin.py:889` 的 `ZoneInfo("Asia/Shanghai")`），不要用 `CURRENT_DATE`（那是 DB 会话时区）。

清理：`CHAT_USAGE_RETENTION_DAYS=90`，清理由 KGD-03 的定时任务顺带做（见 D1，且**不要挂在 10 分钟 timer 上裸跑**，见 D1 修正）。

### B2 · COST-02：`/chat` 用户级日配额

**迁移 026 同批加**：

```sql
CREATE TABLE IF NOT EXISTS chat_usage_day (
    user_key  TEXT     NOT NULL,
    day       DATE     NOT NULL,          -- 由应用传上海日历日；**不设 DEFAULT CURRENT_DATE**
    n         INT      NOT NULL DEFAULT 0,
    PRIMARY KEY (user_key, day)
);

CREATE TABLE IF NOT EXISTS anon_issue_day (   -- /anon 领号的日上限
    ip        TEXT     NOT NULL,
    day       DATE     NOT NULL,
    n         INT      NOT NULL DEFAULT 0,
    PRIMARY KEY (ip, day)
);
```

> ⚠️ `day` **故意不设 `DEFAULT CURRENT_DATE`**：默认值走的是 DB 会话时区，和「今日 = 上海日历日」的口径不一致；
> 不设默认，漏传就报错，比静默写错日期好。

**新增 `security/quota.py`**，一次往返完成自增并返回（**超限时返回空行**）：

```sql
INSERT INTO chat_usage_day (user_key, day, n) VALUES (%s, %s, 1)
ON CONFLICT (user_key, day) DO UPDATE SET n = chat_usage_day.n + 1
WHERE chat_usage_day.n < %s
RETURNING n
```

> ⚠️ **日期由应用传入上海日历日，不要用 `CURRENT_DATE`**（那是 DB 会话时区，与「今日」口径不一致）。
> ⚠️ `WHERE` 挂在 `DO UPDATE` 上：`n` 已达上限时**不更新、不返回行** → 这一次没有把 `n` 加上，被拒的请求不占额度。用 `cursor.fetchone() is None` 判超限。

**挂载点**：`api/chat.py:58-80` `_prepare` 里，身份确定之后（`who` 已解析）立即检查。
返回形态沿用全站统一的 `{"detail": {"code": ..., "message": ...}}`（`Decision` 在 `security/ratelimit.py:33-41` 就是这套 `code`）：

- code `quota_exceeded`、HTTP **429**、带 `Retry-After`（秒到上海时区次日零点）
- 匿名按服务端签发的 `anon_id` 计；`who["role"] == "admin"` 直接放行（`api/system.py:21` 是同样的判定写法）

**计数键只用服务端身份（review_v9 §3.2 关键修正）**：

- **不要读 `payload.user_id`**。`AUTH_REQUIRED=false`（`config.py:219` 默认）时，`api/chat.py:62` 用 `payload.user_id`、缺省字符串 `"anonymous"`——这个字段由客户端填，换一个值就是一个新配额，不填则所有人共用 `"anonymous"` 的 50 次。计数键只用：登录用户的 `user_id`，或服务端 `POST /auth/anon` 签发的 `anon:`。
- **匿名令牌领取本身要有日上限**（第三轮 review §3 要求写死具体值）：`POST /auth/anon`（`api/auth.py:359-366`）应用层现在无次数限制，只受 `/auth/` 的 `60r/m`。做法：按 IP 每天 20 个（`ANON_ISSUE_PER_IP_DAY=20`），复用同一套 `INSERT ... WHERE n < 上限` 写法（表见下），超限返回 429 `code=anon_limit_exceeded`。不限制的话每个新 `anon_id` 都有自己的 50 次，配额只对不肯换号的人有效。
  ⚠️ **IP 必须取 `api/deps.py:37-48` 的 `client_ip()`，不能用 `request.client.host`**（第四轮 review §3.2）：
  `client_ip()` 读 nginx 写入的 `X-Real-IP`（`X-Forwarded-For` 第一段是客户端自己塞的，不能用于频控）；
  用连接对端的话，请求经过 nginx 后所有人都是 `127.0.0.1`，**一天 20 次就会把全站的匿名入口关掉**。
  也不要写 nginx 变量 `$binary_remote_addr` —— 计数在 Python 应用里，那是 nginx 侧的变量，应用读不到。
- `AUTH_REQUIRED=true` 时匿名进不了 `/chat`（`api/deps.py:132-138`），「按 anon_id 计数」这条分支走不到——**计划要以哪种模式验收要写死**（本版按 `AUTH_REQUIRED=true` 验收，`false` 下的 anon 上限是第二道闸）。

配置：`CHAT_QUOTA_PER_USER_DAY=50`、`CHAT_QUOTA_ENABLED=false`（**默认关**，见 §十 review_v9 §4.3：配额和缓存一旦默认开，计数键若有偏差，上线当天作用在全部用户身上）。
`ANON_ISSUE_PER_IP_DAY=20`。

**nginx 的 `10r/m` 必须放宽 —— 改的是 zone 定义，不是引用点**（第三轮 review §3 纠正）：

- 速率定义在 `deploy/nginx/00-nutri-graph.conf:17` 的 `limit_req_zone ... zone=nutri_chat:10m rate=10r/m;` → **改成 `rate=60r/m`**（与 `:18` 的 `nutri_api` 同量级）。
- `deploy/nginx/nutri-graph-locations.conf:49-51` 只是 `limit_req zone=nutri_chat burst=3 nodelay;` 的**引用**，改那里的字不会改速率。
- 目的：同一 NAT 后面第二个人不会在进配额前就被 nginx 打成 429（那种 429 没有 `quota_exceeded`，验收脚本会判失败）。

**验收**：脚本连打 100 次，第 51 次起 429 + `quota_exceeded`；两个用户互不影响；同一 NAT 后不互相挤占；**连领两个匿名身份不能各有 50 次**（由 `/anon` 日上限挡住，写进 `tests/test_chat_quota.py`）。

---

## 六、Phase C：并发与成本削峰

> 顺序锁定：**先 PERF-02（收敛），再 PERF-03（消重）**。
> （原 PERF-01「多 worker 扩容」已移出本计划——它属于参数配置调参，等真有扩容需求时单独评估，见 §一。）

### C1 · PERF-02：把线程放进笼子

1. **SSE 并发上限**：新增模块级 `threading.Semaphore`，`api/chat.py:163` `chat_stream` 里尝试非阻塞获取；拿不到直接返回 503 + `code=stream_busy`（比让线程堆积好得多）。释放放在工作线程的 `finally`。
   ⚠️ **`work()` 现在只有 `try/except`，没有 `finally`**（`api/chat.py:132-156`，review_v9 §4.1 已复验）——清理逻辑在 except 之后。要**在 `work()` 上加 `finally` 再释放信号量**；请求线程里 `acquire` 失败要在 `start()` 之前就返回，避免槽位泄漏。
   配置 `STREAM_MAX_CONCURRENT=8`。
2. **后台任务收口**：把 `graph/nodes.py:738-744`（画像）与 `:747-760`（摘要）的两处 `threading.Thread` 换成统一的小线程池（建议 `utils/spawn.py` 里再做一层，`max_workers=4`）。
   ⚠️ **`ThreadPoolExecutor(max_workers=4)` 的队列默认无限**（review_v9 §4.1）：任务会堆积，只是不同时超过 4 个线程，「饱和就跳过」不会发生。要用**有界队列**，满了就丢掉这次画像/摘要并打日志，主回答不等待。摘要跳过是安全的（`nodes.py:748` 注释已写明「失败不推进水位线，下次重试」）；画像跳过会漏一次更新，可接受。
3. **Redis 连接复用**：`chatbus.py:108-124` 的 `RedisBus.blpop` 现在每次调用都 `aioredis.Redis(...)` 再 `aclose()`。改成模块级单例客户端（Redis password 为空时不建，沿用 `open_bus()` 的 None 逻辑）。
4. **`/chat` 改 async**（可选，优先级低）：把 `api/chat.py:112` 的 `def chat` 改成 `async def`，`get_agent().invoke(state)` 用 `run_in_threadpool` 包裹。
   **诚实说明收益**：这一步主要是把请求线程还给事件循环，**不会单独提升吞吐**——真正的上限还是 `--limit-concurrency 10`。所以它排在最后、可以做也可以不做。

### C2 · PERF-03：两级缓存

新增 `utils/cache.py`（Redis，`open_bus()` 那套连接参数复用）。

**第一级：embedding 缓存**
key `ng:emb:{model}:{sha1(归一化后问句)}`，TTL `CACHE_EMBEDDING_TTL_H=24`。
挂载点：**只在 `rag/embed.py` 的 `embed_query` 一处**（review_v9 §3.3：`nodes.py:157` 和降级路径 `nodes.py:463` 都会走到这里）。再在 `nodes.py:147` 包一层，容易只包住词典未命中的那次，漏掉整句向量。问句归一用已有的 `kg/normalize.py:56` 的 `normalize_text`。
⚠️ Redis 不可用时缓存失败要当成**未命中**，不能让 `/chat` 500。

**第二级：检索结果缓存**
key `ng:graph:{intent}:{cid}:{allergen_hash}:{policy}:{include_prov}:{epoch}`，TTL `CACHE_GRAPH_TTL_S=300`。

> ⚠️ **`allergen_hash` 是排序后再哈希**（第三轮 review §5）：`sha1("|".join(sorted(allergens)))[:12]`。
> 不要把过敏/忌口**原文**拼进 Redis key —— 一是 key 会随忌口数量变长、二是过敏史属于健康敏感信息，
> 不该以明文出现在 Redis 的键名里（`KEYS *` 就能看到）。

⚠️ **三条 review_v9 §3.3 的修正，缺一不可**：

1. **key 必须含忌口哈希**：`graph_query_node` 把过敏和忌口放进每次 Cypher（`graph/nodes.py:358-374`，模板 `kg/query.py:31` 的 `NOT b.canonical_name IN $allergens`）。计划原稿的 key 没有忌口——忌口为空的热点结果被缓存后，下一个对虾过敏的用户命中同一 key，推荐列表里仍有虾。**这是安全洞，不是性能问题。** 用上面的 `allergen_hash`，不拼原文。
2. **只缓存 `_query.run` 的单次结果，key 就是单次 `(intent, cid)`**：一句话可以有多组 `(intent, cid)`（`kg/query.py:175-198`），每次 `run` 单独缓存、单独命中，整句结果由多次命中拼起来。
   ⚠️ 第三轮 review §5 指出：原稿「key 要覆盖整组，或按单意图才缓存」与「只缓存单次 `run`」**是相反的做法**，已删除前半句。
3. **`epoch` 的 `INCR` 位置修正**：计划写「`kg/merge.py` 的 `merge_pending`」，**`merge_pending` 在 `kg/promote.py:658`，`kg/merge.py` 里没有它**。且 `rollback`（`kg/merge.py:380`）和 `scripts/backfill_nutrients.py` 直接改图、不经过 `merge_pending`——只在审核通过时自增，回滚后的几分钟里问答仍是旧图，禁忌边也在这几分钟里。**这三处真正写图的地方都要 `INCR`**。
   ⚠️ **`--dry-run` 不写图，不要 `INCR`**（第三轮 review §5）——干跑时自增会让全站缓存无谓失效一次。

做法是 Redis 里放自增计数器 `ng:graph:epoch`，上述三处写图成功后 `INCR`。key 含 epoch ⇒ epoch 一变，全站旧缓存自然失效，**不用删任何键**。

**第三级：答案缓存（与验收口径对齐，二选一）**
review_v9 §3.3 指出验收和范围是反的：needs 验收要「第二次 LLM 调用不增、p95 降 > 60%」，但计划把答案缓存标成先不做，两级缓存都在 `generate_node` 之前，第二次仍进 `stream_model`。**本版二选一写死**：

- **选项 A（推荐，省事）**：本版验收改成「第二次不再请求 embedding、不再打 Neo4j；LLM 仍调用一次」。答案缓存继续不做。
- **选项 B**：把「无画像、无 session、单一意图」的答案缓存纳入本版，用它来对「LLM 不增」这条验收。

（本计划按选项 A 写，答案缓存标 P2。）

配置：`CACHE_ENABLED=false`（**默认关**，review_v9 §4.3）、`CACHE_EMBEDDING_TTL_H=24`、`CACHE_GRAPH_TTL_S=300`。

**验收**（选项 A）：同一问句第二次，p95 降（embedding + Neo4j 被缓存跳过）；approve 一个新 bundle 后立即能查到新证据（验证 epoch 失效生效）；对虾过敏用户的忌口结果不被「无忌口热点」的缓存污染。

---

## 七、Phase D：图谱治理

### D1 · KGD-03：给补录队列装上驱动器

新增 `deploy/systemd/nutri-graph-fetch.service`（`Type=oneshot`）+ `.timer`：

```
[Timer]
OnBootSec=2min
OnUnitInactiveSec=10min
```

⚠️ **原稿的 `OnUnitActiveSec` + `Persistent=true` 不会自己跑第一次**（review_v9 §3.6）：
- `OnUnitActiveSec` 相对「上一次激活」计时，服务从未跑过时没有起点，`enable --now` 只启用了 timer，第一次抓取不会发生。
- `Persistent=` 的手册说明只作用于 `OnCalendar=`，配在单调计时器上不起作用。
- 改成 `OnBootSec=2min`（开机跑第一次）+ `OnUnitInactiveSec=10min`（每次跑完再等 10 分钟）。

命令：`/opt/envs/nutri/bin/python -m scripts.run_fetch --limit 5 --recover 15`
（`WorkingDirectory=/opt/code/nutri_graph`、`User=root`，与现有 service 保持一致）。

配套两点：

1. **日志单独文件，不要和 Web 进程共用 `backend.log`**（review_v9 §3.6）：两个进程一起 `TimedRotatingFileHandler` 会互相截断。定时任务写 `logs/fetch.log` 或只进 journal。`configure_logging()` 可复用格式，不复用同一文件。`scripts/run_fetch.py:50-53` 的 `logging.basicConfig` 改掉。
2. **「每日一次」的任务不能挂在 10 分钟 timer 上裸跑**（review_v9 §3.6）：
   - 每日 `backfill_nutrients --repair-nodes --dry-run`（KGD-01 误删后的常态化哨兵，产出 `nutri_nutrient_drift` 指标给 A2）
   - 每日清理 `llm_call_log`（`CHAT_USAGE_RETENTION_DAYS=90`）
   这两件另做一个 `OnCalendar=daily` 的 timer，或在命令里用日期文件做门闩。10 分钟扫一次图/删一次库不是「每日」。

防超支已有：`FOOD_FETCH_DAILY_BUDGET`（`config.py:329`）+ `fetch.run()` 的 `budget_exhausted`（`scripts/run_fetch.py:69-71`），定时任务沿用即可，不用改。单任务失败由 `process_task` 吃掉、`run()` 循环继续（`kg/fetch.py:378-382`），「3 个失败任务不堵后面」不用改队列。

**验收**：登记一个新食材，无人干预 30 分钟内产出 bundle 且进待审列表；`nutri_fetch_queue_depth` 指标随之变化。

### D2 · KGD-02：来源快照

1. **新增 `kg/snapshot.py`**：`save(source, external_id, raw: dict) -> (uri, sha256)`。
   **第一阶段落本地磁盘**（`SNAPSHOT_DIR=data/snapshots/`），接口留成可直接换成 OSS/MinIO 的形态，不要提前引入 SDK。
   ⚠️ **`sha256` 要对稳定序列化**（review_v9 §3.7）：`json.dumps` 默认键序会让同一份响应算出两个哈希。要键排序 + 固定编码（`sort_keys=True, ensure_ascii=False` + `encode("utf-8")`）。
   注意 `data/external/` 已经在 `.gitignore`（见 `.gitignore` 末段），快照目录要同样加进 ignore。
2. **改 `kg/fetch.py:183-204` `_persist`**：拿到的 `detail`（USDA 原始响应）先过 `snapshot.save()` 再落 bundle。
3. **迁移 `027_bundle_snapshot.sql`**：`kg_food_bundle` 增加
   `snapshot_uri TEXT`、`snapshot_sha256 TEXT`、`fetched_at TIMESTAMPTZ` 三列（沿用 `IF NOT EXISTS` 风格，`007_promote.sql` 就是这么补列的）。
4. **导入通道是需求第 2 条，不是可选项**（review_v9 §3.7）：`kg/import_bundle.py` 里两种 locator（`:86` 的 `job:{id} 第{row}行`、`:93` 的 `import:{id}#r{row}`），原始 Excel 未保留。**上传的原始 Excel 要走同一个 `snapshot.save()`**，和 USDA 的 `detail` 一样。
5. **管理端快照读接口（具体路径，不是「以后再说」）**：在 `api/admin.py` 加
   `@router.get("/kg/bundles/{bundle_id}/snapshot")`（**不写 `/admin` 前缀**，`router` 已带；对外 URL 是
   `/admin/kg/bundles/{bundle_id}/snapshot`）：
   读 `kg_food_bundle.snapshot_uri` → 读文件 → 重算 `sha256` 与 `snapshot_sha256` 比对
   （不一致时返回 `code=snapshot_mismatch`，但仍把内容返回并标注）→ 返回原始 JSON。
   前端入口在 `admin-web/src/views/BundleDetail.vue` 加「查看原始快照」按钮。
   前台依据今天还是字符串列表（`frontend/assets/app.js:871-876`），本版**只做管理端**，前台指向快照列为后续。
6. **复检**（P2，本 phase 只留接口）：`scripts/check_sources.py` 比对快照与当前值的差异，季度跑一次，结果给管理员看。依赖 D1 的定时器，那条没通之前脚本跑不起来——在「明确不做」里写一句，避免被当漏项。

**验收**：任一条补录出来的边，能在后台一键打开抓取当时的原始快照并校验 sha256；把某条 URL 人为改错，那条边依然能自证。

---

## 八、迁移与配置清单

### 迁移（当前最新 `025_sms_delivery.sql`）

| 文件 | 内容 |
|---|---|
| `026_llm_usage.sql` | `llm_call_log`（账本 + span 字段：`span_id/parent_span_id/kind/node/input/output`）+ `chat_usage_day` |
| `027_bundle_snapshot.sql` | `kg_food_bundle` 加 `snapshot_uri / snapshot_sha256 / fetched_at` |

⚠️ 迁移顺序沿用项目既有约定：`python -m scripts.migrate` → vector 扩展 → `init_pg`。
`026` 是纯业务表，不涉及 vector，可在 `migrate` 一步生效。
⚠️ 026 的索引**不要建 `(ts::date)` 表达式索引**（会 migrate 失败，见 B1）。

### 新增配置项（同步进 `.env.example` —— review_v8 §4.2 已经吐槽过它缺 `SMS_PROVIDER` 等旧账）

| 变量 | 默认 | 用途 |
|---|---|---|
| `METRICS_ENABLED` | `true` | /metrics 开关（风险小，默认开） |
| `METRICS_CACHE_TTL_S` | `30` | 抓取保护 |
| `CHAT_QUOTA_ENABLED` | `false` | COST-02（review_v9 §4.3：默认关，避免上线当天作用全部用户） |
| `CHAT_QUOTA_PER_USER_DAY` | `50` | COST-02 |
| `CHAT_USAGE_RETENTION_DAYS` | `90` | 账本清理 |
| `STREAM_MAX_CONCURRENT` | `8` | PERF-02 |
| `BG_WORKERS` | `4` | PERF-02 后台线程池 |
| `CACHE_ENABLED` | `false` | PERF-03（review_v9 §4.3：默认关） |
| `CACHE_EMBEDDING_TTL_H` | `24` | PERF-03 |
| `CACHE_GRAPH_TTL_S` | `300` | PERF-03 |
| `SNAPSHOT_DIR` | `data/snapshots` | KGD-02 |
| `TRACE_ENABLED` | `false` | OBS-03：关时只写 `kind='llm'` 的 token 行（入参留空），不写 node/tool 行 |
| `ANON_ISSUE_PER_IP_DAY` | `20` | COST-02：`/anon` 领号日上限 |
| `LLM_PRICE_MAP` | 按模型名的映射 | COST-01：`{模型: {in, out}}`，单位元/千 token；缺项记 0 并 warning |

⚠️ **删除项 —— 摘 Langfuse 会断掉的引用（第三轮 review §4.5 全量清单，缺一个就 `ImportError`）**：

| 位置 | 现在在用什么 | 处理 |
|---|---|---|
| `observability/tracing.py` | 整个模块（`get_client` / `trace` / `record_result` / `redact_trace` / `record_score`） | 删；`record_score` 无其他调用方，一并删 |
| `api/chat.py:85` | `from observability.tracing import record_result, redact_trace, trace` | 改为 `llm/usage.py` 的新接口 |
| `config.py:173-176` | `LANGFUSE_ENABLED / PUBLIC_KEY / SECRET_KEY / HOST` | 删 |
| `.env.example:90-93` | 同上四个变量 | 删 |
| `requirements.txt:48` | `langfuse>=3.0.0,<4.0.0` | 删 |
| `tests/test_chat_stream.py:324` | `monkeypatch.setattr("observability.tracing.trace", _no_trace)` | 改到新函数 |
| `tests/test_sms_delivery.py:224` | 同上 | 改到新函数 |
| `tests/test_sensitive.py:351-367` | `test_redact_trace_keeps_guard_output` 直接测 `redact_trace` | **改成断言 PG 里该 `rid` 行的 `input`/`output`**（这是打码两步法的回归测试） |

---

## 九、测试计划

| 新增用例 | 验证什么 |
|---|---|
| `tests/test_trace_id.py` | rid 贯穿主线程与后台三个线程；日志里能 grep 到；SSE 事件带 rid |
| `tests/test_metrics.py` | `/metrics` 含全部指标；连续两次请求后 `nutri_chat_total` 递增且 `graph_hit_ratio` 计算正确 |
| `tests/test_health_redis.py` | Redis 不可达时 `/health` 转 503 且指明是 redis（mock 连接） |
| `tests/test_llm_usage.py` | 一次调用写一行 `llm_call_log`，user/purpose 正确；未埋点的记为 `unknown` 而非丢失；**流式回答只记一笔（不是两笔）**；`pool_chain`（segment/profile）也记账；失败记 `ok=false` |
| `tests/test_usage_reset.py` | `check_llm_pool --reset` 之后历史 token 累计仍在 |
| `tests/test_trace_span.py` | 一次 `/chat` 产出 span 树：根 `node=chat` **且 `kind='node'`**（不是 `llm`）→ `kind=llm` 的 answer span + `kind=tool` 的 web_answer/link span；**`parent_span_id` 真的指向根（不是空串）**；树的父子关系与实际走过的节点一致（web_answer 分支不经过 assemble/generate）；多轮按 `session_id` 可串 |
| `tests/test_span_redact.py` | **命中敏感词时 `input`/`output` 落库为占位符、不含原文**（含**已启动的画像线程**后插的行）；`guard` 命中保留 output；未命中时正常存；超 2048 字被截断 |
| `tests/test_admin_stats_kind.py` | `today_calls` / `today_tokens` / `today_cost` / `top_users` **只统计 `kind='llm'`**：造一次含根 + tool span 的请求，调用次数不应多加 |
| `tests/test_trace_detail.py` | `/admin/trace?rid=` 返回完整 span 树；`/admin/trace/sessions?session_id=` 只返回轮次摘要（不拉全量） |
| `tests/test_chat_quota.py` | 第 51 次 429 + `quota_exceeded`；两用户独立；admin 豁免；**被拒的请求不占成功额度**；**连领两个匿名身份不能各有 50 次**（`/anon` 日上限） |
| `tests/test_bg_pool.py` | 后台线程池饱和时跳过可选任务、主回答不受影响；有界队列不堆积 |
| `tests/test_stream_concurrency.py` | 超过 `STREAM_MAX_CONCURRENT` 立刻 503 `stream_busy`，不堆积 |
| `tests/test_cache.py` | 二级缓存命中后不重复请求 embedding/Neo4j（验收按选项 A：LLM 仍调一次）；epoch 自增后旧 key 不再命中；**忌口不同的两用户命中不同 key** |
| `tests/test_snapshot.py` | 抓取产出快照文件 + sha256 入 bundle 行；**同一份响应两次 `save()` 的 sha256 一致**（稳定序列化） |

跑法沿用既定规矩（项目记忆「测试铁律」）：**本地不跑**，改完 scp 到远程 `/opt/code/nutri_graph`，用
`/opt/envs/nutri/bin/python` + 独立库 `nutri_graph_test`，并且**不要对着生产图开 `ALLOW_GRAPH_TESTS`**。

---

## 十、上线与回滚

每个 Phase 都有**总开关**，出问题改配置重启即可，不需要回滚代码：

| Phase | 一键关闭 |
|---|---|
| A | `METRICS_ENABLED=false`；`rid` 中间件是**纯 ASGI 层**，回滚方式是「开关关闭时直接把请求交给下一层」或去掉这层 ASGI wrapper（⚠️ **不要写「注掉 `@app.middleware`」**——A4 已明确禁用 `BaseHTTPMiddleware`，两处说法不能并存） |
| B | `CHAT_QUOTA_ENABLED=false`（配额，默认本就关）；`TRACE_ENABLED=false`（只停 node/tool 行与入参，`kind='llm'` 的 token 行照写）；账本埋点失败已设计成吞异常不阻断主流程 |
| C | `STREAM_MAX_CONCURRENT` 设成很大值等价于不限流；`CACHE_ENABLED=false` 全关缓存 |
| D | `systemctl stop nutri-graph-fetch.timer`（队列回到手动，功能不受影响） |

部署步骤（沿用既有流程）：

```bash
# 1) 后端
scp -r <改动> root@8.134.253.167:/opt/code/nutri_graph/   # 源码全量覆盖，不要只传改动文件
# 2) 迁移
cd /opt/code/nutri_graph && /opt/envs/nutri/bin/python -m scripts.migrate
# 3) 测试库验证后重启
systemctl restart nutri-graph
# 4) 管理端前端（本版有 Overview/Trace/BundleDetail 改动）
cd admin-web && npm run build
rsync -a --delete admin-web/dist/ root@8.134.253.167:/var/www/nutri_graph/console/   # 整目录，不是只传 index.html
# 5) Phase D + A2 的定时器（抓取 / 每日任务 / 指标上报）
systemctl daemon-reload
systemctl enable --now nutri-graph-fetch.timer nutri-graph-metrics.timer nutri-graph-daily.timer
```

⚠️ **A2 的指标上报要落成一个命令 + 一个 timer**（第三轮 review §5）：本计划要求「读 `/metrics` → 调阿里云自定义监控上报」，
但这只是描述、不是步骤。做法：新增 `scripts/report_metrics.py`（读本地 `/metrics`，转成自定义监控的 MetricList 上报），
配 `nutri-graph-metrics.timer`（**`OnBootSec=2min` + `OnUnitInactiveSec=1min` + `AccuracySec=5s`**，
与 A2 及抓取 timer 完全一致 —— 只写 `OnUnitActiveSec` 的话 `enable` 之后第一次不会来），和抓取 timer 一起 `enable`。
`/health` 的站点监控**不依赖它**（那条直接看状态码）。

⚠️ 前端如果有改动，记得：主站静态根目录是 `/var/www/nutri_graph`（不是 `/opt/code/nutri_graph/frontend`），
且 `/assets/` 是 7 天强缓存，改 js/css 必须 bump `index.html` 里的 `?v=`；
管理端是 `/var/www/nutri_graph/console/`，`index.html` 已是 `no-store`，但**必须整目录覆盖**（见上）。

---

## 十一、明确不做（防过度工程）

- **不引入 Prometheus 全家桶 / Grafana**：先输出文本指标 + 云监控告警，够用且零运维成本。
- **不做全面的异步化改造**：只把 `/chat` 换成 async def，收益有限且引入一次链路风险，详见 C1 第 4 点的说明。
- **不在这一版接「第三人内容安全」**（`CONTENT_MODERATION_PROVIDER` 已有开关，保持 `none`）。
- **不重写补录队列**：`kg_fetch_task` 的状态机已经完备，D1 只是补一个驱动器。
- **不换 Milvus / GraphRAG 框架**：生态兼容成本高（`ragas × langchain-community` 的补丁就是教训）。
- **不引入 Langfuse**：OBS-03 定摘除，`observability/tracing.py` 的 Langfuse 依赖与 `config.py` 的 `LANGFUSE_*` 一并移除，span 统一走自建 PG 表。
- **本版不做多 worker 扩容（原 PERF-01）**：属于参数配置调参，等真有扩容需求时单独评估。

---

## 十二、待确认（影响实施细节）

1. **月成本红线** —— 有这个数字，`CHAT_QUOTA_PER_USER_DAY` 才能从拍脑袋的 50 变成算出来的值。建议先按 50 上线一周，再用 B1 的 `top_users` 分位数反推。
2. **SSE 超限时是「立刻 503」还是「排队等待」** —— 我选前者（明确失败优于无感知排队），但如果你更看重「别让用户看到报错」，可以改成短队列 + 进度提示，代价是多一个队列结构。
3. **`STREAM_MAX_CONCURRENT` 与 `--limit-concurrency` 的关系** —— 两者现在是两个独立的数。建议上线前一起标定一次压测，把关系式写进 `deploy/systemd` 的注释（现在那里已经写了 `--workers` 与连接池的关系，风格延续即可）。

---

## 十三、review_v9 评审结论（四轮复核后）

> 复核对象：[`docs/review/review_v9.md`](../review/review_v9.md)，现为**第四轮**（78 行）。
> 复核方式：逐条回到源码实测（读码 + `sed` 取行 + `grep`），不采信评审文字本身。
>
> **编号说明（避免混淆）**：评审文件自己标注的「第三轮 / 第四轮」是按**计划修订次数**算的；
> 本文 §13 按**我复核的时间顺序**称「第一轮 / 第二轮 / 第三轮」，对应关系：
> 评审「第三轮」= 本文第二轮（10:42）；评审「第四轮」= 本文第三轮（11:06）。
> 正文里引用「第三轮 review §x」时，指的是**评审文件自标为第三轮**的那一份（即本文的第二轮）。

### 13.1 总评

| 维度 | 判定 |
|---|---|
| **最新一轮（11:06）：结论「可以开工」** | **认同**。它核的 8 项上一轮问题里 6 项判 ✅、2 项给了具体修法（见 §13.7）；抽查 4 个锚点全部对得上 |
| **最新一轮：3 处「照抄会错」的示例** | **3/3 成立**，且都是**我上一轮新写进去的片段**，不是设计错 |
| 10:42 那一轮：4 条「还不能开工」的理由 | 4/4 成立，其中 3 条直指我「标了没改」 |
| 10:42 那一轮：§4 的五处 OBS-03 缺口 | 5/5 成立 |
| 08:29 那一轮：6 条硬伤 | 6/6 成立（见 §13.3） |

**一句话：四轮走下来，08:29 补了我漏掉的 OBS-03，10:42 查了我「声称已改」的真伪，11:06 抓了我新写片段的 bug —— 三次都查对了，到这轮才放行。**

> **结论：可以开工。** 但记住上一轮的教训——**只有正文改了才算改了**，§13 的表格本身不构成施工依据（评审第一次就说：§十三 不代替 §四到 §十）。

### 13.2 第二轮评审（10:42）逐条实测：我「标了已改、正文没改」

| # | 评审指控 | 实测方式 | 判定 |
|---|---|---|---|
| 1 | 配额 SQL 仍用 `CURRENT_DATE` 且无条件 `n = n + 1` | 读 `plan_v9.md` B2 原文，确实是旧 SQL | ✅ **成立，我标了「已改」但没改** |
| 1b | `60r/m` 写的是改 `locations.conf`，速率实际在 zone 定义 | `deploy/nginx/00-nutri-graph.conf:17` 才是 `limit_req_zone … rate=10r/m`；`locations.conf` 只有 `zone=` 引用 | ✅ **成立，改错文件了** |
| 2 | span 挂不到父节点 | `record()` 设计里只取 `rid/user_id/session_id`，没有「当前 span」的 contextvar → `parent_span_id` 恒为空串 | ✅ **成立，树根本连不起来** |
| 2b | 根节点用了 `kind` 默认值 `llm`，统计又没过滤 | SQL 是 `kind … DEFAULT 'llm'`；根节点若不明写，每次提问多一行 0 token；`/admin/stats` 四查询未带 `kind='llm'` | ✅ **成立，费用会被虚高** |
| 3 | 打码晚于插入 | `on_success` 在调用过程中插入；`api/chat.py:101-108` 的 `_redact` 判定在 `_invoke` 返回**之后** | ✅ **成立，只做一步必漏** |
| 3b | 验收第一条不是真实路径 | `graph/workflow.py:79` `web_answer → memory_write`，与 `assemble/generate` 并列；不存在 `assemble → generate → web_answer` | ✅ **成立，需求原稿的链是错的** |
| 3c | `on_starved` 也有 token，不记则少一笔 | `middleware.py:209`、`:238`（同步空正文重试）；`agent.py:141`（流式） | ✅ **成立，补进账本** |
| 3d | `pool_chain()` 只返回链，看不到 token | `llm/router.py:184` 函数体只有 `with_fallbacks` 与缓存，无调用；记账必须在 `segment.py:55` / `boundary.py:353` | ✅ **成立** |
| 4 | 详情页是 Vue，不是加 JSON | `admin-web/src/views/Overview.vue`、`router.js`、`views/Layout.vue`、`views/BundleDetail.vue` 均存在，但没有 trace 页 | ✅ **成立** |
| 4b | 快照一键打开没有路由 | D2 原文只写「计划没这条路由」，自己也没给路径 | ✅ **成立，我给的是抱怨不是方案** |
| 5 | 摘 Langfuse 会断的引用不止两处 | `.env.example:90-93` 四个 `LANGFUSE_*`；`tests/test_chat_stream.py:324`、`tests/test_sms_delivery.py:224` monkeypatch；`tests/test_sensitive.py:351-367` 直接测 `redact_trace`；`requirements.txt:48` | ✅ **成立，会 `ImportError`** |
| 6 | §十 回滚还写「注掉 `@app.middleware`」，与 A4 禁用 `BaseHTTPMiddleware` 矛盾 | 两处并存属实 | ✅ **成立** |
| 7 | C2 里「覆盖整组」与「只缓存单次 run」相反 | 两句自相矛盾 | ✅ **成立，已删前半句** |
| 8 | `--dry-run` 不该 `INCR` | 干跑不写图，自增会让全站缓存无谓失效 | ✅ **成立，已补注** |

### 13.3 第一轮评审（08:29）逐条实测

| 第一轮 review 断言 | 复核方式 | 判定 |
|---|---|---|
| `pool_chain` 不经过 `on_success` | `router.py:184` 原生 `with_fallbacks`；调用点 `segment.py:54`、`nodes.py:720` | ✅ 成立 |
| 成功路径两处埋点记两笔 | `on_success` 定义 `middleware.py:158`，被 `:215`/`:244`/`agent.py:139` 调用 | ✅ 成立 |
| `(ts::date)` 索引迁移失败 | `timestamptz→date` 是 STABLE，表达式索引要 IMMUTABLE | ✅ 成立 |
| 图缓存 key 缺忌口会串用户 | `nodes.py:359` 组装 allergens；`query.py:31` 模板含 `NOT … IN $allergens` | ✅ 成立 |
| `merge_pending` 在 `promote.py:658` | `grep` 确认；`merge.py` 无此函数 | ✅ 成立 |
| `work()` 无 finally | `api/chat.py:132-156` | ✅ 成立 |
| `/metrics` 出不了 nginx | 有 `location = /health`，无 `location = /metrics` | ✅ 成立 |
| `/admin/stats` 缺表留 null | `admin.py:84-87` `pending=None` vs `:136-140` 503 | ✅ 成立 |
| `--reset` 清掉 token 累计 | `pool.py:342-351` 重建 `_states` + `unlink()` | ✅ 成立 |
| `/chat` 实际 `:112`/`:163` | `grep "@router.post"` | ✅ 评审对 |
| 导入 locator 是两种 | `import_bundle.py:86` / `:93` | ✅ 评审对 |
| 画像调用在 `boundary.py:353` | 该行 `resp = llm.invoke(prompt)` | ✅ 成立 |

### 13.4 两条已失效条款（计划变了，评审前提消失）

| review 条款 | 为何失效 |
|---|---|
| 第一轮 §3.4「PERF-01 共享文件上的多进程」 | **PERF-01 已整体移出本计划**（属参数配置调参）。技术判断本身正确（`os.replace` 覆盖同一文件、别名缓存是进程内单例），但没有实施对象。将来重启扩容可直接复用。 |
| 第一轮 §3.1「Langfuse 不用另做导出」 | **Langfuse 已定摘除**。观察（trace metadata 无 token）正确，结论从「不用导出」升级为「整个摘掉」。 |

### 13.5 OBS-03 风险闭环（我自查的 4 条，10:42 那轮评审已全部修正）

第一轮 review 没覆盖 OBS-03，我自查补了 4 条风险；**第三轮 review 把这 4 条全部推到「可施工」**：

| 第一轮自查风险 | 第三轮指出的缺口 | 现在的处理（已在正文） |
|---|---|---|
| ① 敏感信息二次落库 | 只做「插入时判断」盖不住已写下的行，也盖不住画像线程随后插入 | **两步法**：`_redact` 置位写 contextvar（管未来）+ 返回后按 `rid` UPDATE（管过去）；`guard` 保留 output。打码范围**只用现有 `_redact`**，不扩成所有健康问句 |
| ② 写放大 | 截断/吞异常方向对，但没写 `TRACE_ENABLED` 的真实语义 | 截断 2048 字 + 吞异常；`TRACE_ENABLED=false` 时**只跳过入参与 node/tool 行，`kind='llm'` 的 token 行照写**；默认 `false` |
| ③ 表名名不副实 | 我提了「建议改名」，反而造成双口径 | **统一用 `llm_call_log`**（改名列为可选后续，要改必须回写 needs）；但 **`kind='llm'` 过滤改为强制**，与改名无关 |
| ④ 多轮树过大 | 方向对，但「点开能看到」缺页面 | 补 Vue 施工清单（`Trace.vue` + `router.js` + `Layout.vue`）+ `console/` **整目录**部署 |

### 13.6 10:42 那轮（评审自标「第三轮」）的落地位置

> 第一轮我犯的错就是在这里写「✅ 已改」而正文没动。这次每一行都可回指到具体条款。

| 第三轮 §6 步骤 | 落在了哪 | 核对锚点（可直接搜） |
|---|---|---|
| 1. B2 SQL + zone 速率 + `/anon` 上限 | §五 B2 | `WHERE chat_usage_day.n < %s`、`00-nutri-graph.conf:17`、`ANON_ISSUE_PER_IP_DAY` |
| 2. 当前 span / 根 `kind='node'` / 统计过滤 / `TRACE_ENABLED` | §五 B1 | `_current_span`、`kind='node'`、`WHERE kind = 'llm'`、默认 `false` |
| 3. 打码两步 + `guard` 保留 output + 测试改到 PG 行 | §五 B1 | `第一步（管未来）`、`第二步（管过去）`、`test_sensitive.py:351-367` |
| 4. needs 验收第一条改成「实际走过的节点」 | needs_v9 §八 | `实际走过的节点`、`workflow.py:79` |
| 5. 概览费用 / trace 页 / 快照按钮 / `console/` 整目录 / 价格进配置 | §五 B1 页面表、§七 D2、§八 配置、§十 部署 | `admin-web/src/views/Trace.vue`、`rsync -a --delete`、`LLM_PRICE_MAP` |
| 6. 删 `.env.example` 的 `LANGFUSE_*`、改三处测试 | §八 删除项表 | `删除项 —— 摘 Langfuse 会断掉的引用` |

### 13.7 最新一轮评审（11:06）：可以开工，但 3 处示例照抄会错

本轮评审结论是**「可以按这一版开工」**，只要求先改三处**新写进去的片段**。我逐条实测，**3/3 成立**：

| # | 评审指控 | 实测 | 处理 |
|---|---|---|---|
| 1 | **`span()` 若在进入时写行，父节点指向自己** | 原示例写着「收尾写这一行（或按需在进入时写）」——两句并列；进入时 `_current_span` 已等于 `sid` | ✅ 成立。删掉「进入时写」，明确**只在 `reset` 之后、用进入前保存的 `parent` 写**；已写进 B1 并附原因 |
| 2a | **`/anon` 用连接对端 → 全站共用 `127.0.0.1`** | `api/deps.py:37-48` 的 `client_ip()` 读 `X-Real-IP`（nginx 覆盖写入），才拿到真实 IP；`request.client.host` 经反代后恒为本机 | ✅ 成立。已改为 `client_ip()`，并说明为何 `$binary_remote_addr`（nginx 变量）在应用里也拿不到 |
| 2b | **指标 timer 只写 `OnUnitActiveSec` → 第一次不会来** | A2 原本有 `OnBootSec`，§十 只剩 `OnUnitActiveSec`，两处不一致；且与 D1 自己的结论相反 | ✅ 成立。两处统一为 `OnBootSec=2min` + `OnUnitInactiveSec=1min` + `AccuracySec=5s`（用 Inactive 是「跑完再等」，与抓取 timer 同语义；`AccuracySec` 默认 1 分钟会让 1 分钟间隔整体漂移） |
| 3a | **needs PERF-03 验收仍是「LLM 调用不增加」** | needs 原文如此；计划按选项 A（第二次仍调模型） | ✅ 成立。已改成「不再请求 embedding、不再打 Neo4j」 |
| 3b | **needs OBS-03 验收仍要求「与边一致」** | 正文写「与 `workflow.py` 的边一致」，注脚又说本版不做节点 span —— 自相矛盾 | ✅ 成立。已改成「根 `kind='node'`，llm/tool 的 `parent_span_id` 指向它；走网页参考时有 `web_answer` 的 tool span，不要求出现 `assemble`」 |
| 3c | **两个标量价格盖不住多模型** | `LLM_MODEL_POOL` 有多个模型，`MODEL_PRICE_IN/OUT` 单值会让 `today_cost` 与按金额的 Top 10 都偏 | ✅ 成立。改为 `LLM_PRICE_MAP`（按模型名映射，输入/输出价分开，单位元/千 token），**缺项记 0 并 warning** |
| 3d | **admin 路由会变成 `/admin/admin/trace`** | `api/admin.py:34` 是 `APIRouter(prefix="/admin")` | ✅ 成立。改成装饰器写 `/trace`、`/trace/sessions`、`/kg/bundles/{bundle_id}/snapshot`，并列出「公开 URL ↔ 装饰器路径」对照表 |

**抽查锚点也全部对得上**（评审 §2 末段）：`on_success` 在 `llm/middleware.py:158`；`web_answer → memory_write`（`workflow.py:79`）；
admin router prefix 已是 `/admin`；`client_ip()` 用 `X-Real-IP` 而非 `X-Forwarded-For`。

### 13.8 四轮之后的收尾判断

- **可以开工**。四轮问题全部落在正文，且每条都带可搜索锚点（§13.6 / 本节的表）。
- **仍然不在本版的两件**：PERF-01 多 worker（参数配置）、答案缓存（等成本曲线）。这两条不是遗漏，是 §十一 的明确选择。
- **下次改计划时的自检**：① 新写的代码片段要自己走一遍执行顺序（本轮 3 处全是「片段本身有 bug」，不是设计错）；
  ② 一个事实只写一处数字（timer 在 A2 和 §十 两处就出现了不一致）；③ 文档之间的一致性（needs 验收 vs 计划口径）。
