# needs_v9 —— 成本 / 性能 / 可观测 / 图谱治理 九项现状与需求

> 生成时间：2026-10-31 · 基线 commit `6116bd0 (v8)`
> 来源：`docs/brain/brain_v1.md` 的差距清单，编号沿用（COST/PERF/OBS/KGD），便于双向对照。
> 性质：这是**需求文档**（现状 + 缺口 + 验收口径），不是实施方案。方案留给 plan_v9。
> 所有「现状」都标了 `文件:行`，可复核；写 plan 时以本文为依据，不要凭印象重写。

---

## 一、总览

| 编号 | 一句话现状 | 优先级 |
|---|---|---|
| COST-01 | 有模型维度的 token 累计，但没有用户/时间/费用维度 | P0 |
| COST-02 | `/chat` 只有 nginx 的 IP 限流，没有用户级配额 | P0 |
| PERF-02 | 每条 SSE 一个线程，每轮问答再生两个后台线程，无并发上限 | P1 |
| PERF-03 | 全链路零缓存，重复问题重复付费 | P1 |
| OBS-01 | 只有 tracing，没有 metrics / 告警，`/health` 还漏了 Redis | P0 |
| OBS-02 | 没有 HTTP 层的请求标识 | P1 |
| OBS-03 | 全链路跟踪：多轮对话与「模型/工具」的入参出参耗时不可在后台观察 | P1 |
| KGD-02 | 来源只有 URL 与 locator 指针，没有快照、没有复检 | P1 |
| KGD-03 | 补录队列没有驱动器，靠管理员手点 | P1 |

---

## 二、COST-01 · 计量口径不全：只有模型维度的 token 累计

### 现状

**已经有**（这部分比想象中好，别重复造）：

- 调用成功时会把用量记一笔：
  - 同步路径：`llm/middleware.py:59-66` 的 `_usage()` 读 `usage_metadata.input_tokens/output_tokens`，取不到时回退 `response_metadata.token_usage` 的 `prompt_tokens/completion_tokens`；`middleware.py:160` 调 `get_router().mark_success(spec, *_usage(msg))`。
  - 流式路径：`llm/agent.py:84-100` 用 `stream_usage=True` 读最后一个非空用量，`agent.py:128-139` 构造带 `usage_metadata` 的 `AIMessage` 再 `plan.on_success(...)`；拿不到用量时打 `记 0 token` 的 warning。
  - `--probe` 路径同样记（`llm/router.py:250`）。
- 累计逻辑：`llm/pool.py:282-294` `mark_success()` 累加 `ModelState.calls / prompt_tokens / completion_tokens`，顺便 `self._save()` 落盘。
- 落盘位置：`data/llm_pool_state.json`（`config.LLM_POOL_STATE_PATH`），**该文件在 `.gitignore` 里**。
- 可见入口：`python -m scripts.check_llm_pool`（表格里有「调用」「tokens」两列）、`GET /llm/pool`（`api/system.py:18-34`，`AUTH_REQUIRED` 下仅 admin 可见）。

**缺的**（这才是我把它列为 P0 的原因）：

| 缺什么 | 具体表现 |
|---|---|
| 时间维度 | 只有**累计值**，没有每笔事件的 `(时间, 用户, 会话, 模型, in, out)` → 「今天花了多少」「这一小时谁在刷」都答不出，只能看一个从上次 reset 到现在的总数 |
| 用户 / 会话维度 | 数字挂在模型名下，不知道消耗来自哪个用户、哪个接口（主回答 / 画像抽取 / 会话摘要 / 网页答案都混在一起） |
| 费用维度 | 没有价格表，token ≠ 元，和真实账单对不上账 |
| 持久化 | 存在被 gitignore 的 JSON 里，`scripts.check_llm_pool --reset` 会**连 token 计数一起清零**（它本来只是想把移出池的模型放回池） |
| 多进程一致性 | 计数在进程内存 + 单文件（`llm/pool.py:283` 的 `self._lock`），一旦按 OPS-01 扩到多 worker，会变成每人一份且互相覆盖 |

### 需求

1. **落事件表（兼作全链路 span 表，见 OBS-03）**：每笔 LLM 调用写 `(时间, user_id, session_id, 模型, in_tokens, out_tokens, 用途)`。「用途」至少区分：主回答 / 画像抽取 / 会话摘要 / 网页答案 / 评估。**这张表同时是 OBS-03 的 span 表**，除计费字段外再带一组 span 字段（`trace_id/rid、span_id、parent_span_id、kind、node、input、output`），让「账本」和「调用链树」共用一张表，避免先建纯账本表、OBS-03 再回来改 schema 的返工。
2. **后台能看**：`/admin/stats` 增加「今日调用次数 / 今日估算费用 / Top 10 用户」。
3. **和 `--reset` 解耦**：清理「额度被移出池」状态不能顺手删掉计费累计；两者用不同文件或不同字段。
4. ~~顺手确认 Langfuse 侧已经在记的 usage 能不能直接导出做账~~ **（已定：不引 Langfuse，统一走自建 span 表，见 OBS-03。原 `observability/tracing.py` 的 Langfuse 依赖本版摘除。）**

### 验收口径

- 任意时刻能在后台看到「今天花了多少钱、谁花的」，与实际账单误差 < 10%。
- 执行 `check_llm_pool --reset` 后，历史 token 累计仍在。
- 故意构造一个用户连发 50 次相同问题，能在明细里看到这 50 笔并算出它的占比。

---

## 三、COST-02 · `/chat` 没有用户级配额

### 现状

- **唯一在岗的防线是 nginx**：
  - `deploy/nginx/00-nutri-graph.conf`：`limit_req_zone $binary_remote_addr zone=nutri_chat:10m rate=10r/m;`
  - `deploy/nginx/nutri-graph-locations.conf`：`location /chat { limit_req zone=nutri_chat burst=3 nodelay; limit_req_status 429; }`
  - 即**单 IP 每分钟 10 次、突发 3**，且只按 IP 算。
- **应用层已有的限额都不覆盖 `/chat`**（`config.py` + `security/ratelimit.py` 全量清单）：
  - `FOOD_REQUEST_DAILY_LIMIT_PER_USER` / `FOOD_REQUEST_GLOBAL_DAILY_LIMIT`（未收录登记）
  - `FOOD_IMPORT_DAILY_LIMIT_PER_IP` / `FOOD_MANUAL_DAILY_LIMIT_PER_IP`（Excel 导入 / 手工录入）
  - `FOOD_FETCH_DAILY_BUDGET`、`FDC_*`（抓取）
  - `WEB_ANSWER_DAILY_BUDGET` / `WEB_ANSWER_USER_HOURLY`（网页答案）
  - `SMS_MAX_*`（短信四维频控 + 全局熔断，`security/ratelimit.py:55-126`）
- `security/ratelimit.py` 那套「手机号 / IP / 设备 / 全局」四维设计本身很完整，但它**只服务短信**，`/chat` 不在里面。

### 风险

- `AUTH_REQUIRED=true` 之后，注册用户连续换 token 就能穿透 IP 限流（IP 是同一个，但每次都是合法登录请求）→ 直接作用于模型账单。
- 反方向也会误伤：办公室 / 校园出口 IP 后面是一堆正常用户，`10r/m` 会让同一层楼的人互相挤掉。

### 需求

1. `/chat` 增加**用户维度日配额**（建议起步 50 次/天），超限返回 429 + `code=quota_exceeded`（复用 `Decision.code` 的命名风格）。
2. 匿名走 `anon_id` 计数，管理员可豁免。
3. IP 限流**保留但放宽**，把它降级成「防扫号/防爆破」的兜底，而不是唯一闸门。
4. 建议复用 `sms_send_log` 那种「按天窗口计数」的写法，天然不需要 Redis（与 `security/ratelimit.py:15` 那条注释的取舍一致）。

### 验收口径

- 同一用户脚本连发 100 次，第 51 次起 429 且带 `code=quota_exceeded`；不同用户互不影响。
- 同一 NAT 后面的两个正常用户，各自配额独立、不被彼此触发。

---

## 四、PERF-02 · 每条 SSE 一个线程，没有并发上限

### 现状

一次 `/chat` 请求实际可能起 **3 个线程**，全部 `daemon=True`、全部没有上限：

| 线程 | 出处 | 生命周期 |
|---|---|---|
| SSE 工作线程 | `api/chat.py:158`（`name=f"chat-{stream_id[:8]}"`） | 每条 `/chat/stream` 请求一条，跑完整条链路 |
| 画像抽取 | `graph/nodes.py:738-744`（`name="profile-extract"`） | 每轮问答一条，内部再打一次 LLM + 写 PG |
| 会话摘要 | `graph/nodes.py:747-760`（`name="session-compress"`） | 每轮问答一条，同样要取 PG 连接 |
| （另有）分词 | `kg/segment.py:75` | daemon 线程 |

- **全仓没有 `Semaphore`、没有 `ThreadPoolExecutor`**（唯一一个池是 `kg/websearch.py:98` 的 `max_workers=1`，用途是给外部搜索加超时）。
- `--limit-concurrency 10` 管不到这些自建线程——`config.py:50-56` 的注释自己写了：「并发 10 条在途请求 ≈ 10 条连接，剩下的 5 条留给后台线程（画像抽取 `_defer_profile`、摘要 `compress_if_needed` —— 它们不受 `--limit-concurrency` 限制，同样要取连接）」。
- SSE 读取侧还有个额外开销：`chatbus.py:108-124` 的 `RedisBus.blpop()` **每次调用都新建一条 Redis 连接**，读完 `aclose()`。轮询频率 = 每次事件等待一轮连接建立/释放。

### 风险

needs_v8 第二条的流式改造全量上线后，N 个并发 SSE ⇒ 最多 3N 个线程争抢 15 条 PG 连接，表现为**随机 `pool_busy` 503**（而且这种 503 会被前端当成「系统繁忙」，排查时看不出和 SSE 有关）。这是「上线后才爆炸」的典型。

### 需求

1. **给 SSE 加显式并发上限**：信号量或 `open_bus()` 里的配额，超限快速失败并给出「当前排队人数多」的提示，而不是让线程无限堆积。两个工作台（工作线程、SSE 读连接）都要算进去。
2. **后台两个线程改为受控执行**：至少给 `_defer_profile` / `_defer_compress` 加一个共享的小线程池 + 队列上限；超限时宁可跳过画像抽取（它本来就是「失败了也不影响回答」的定位，见 `nodes.py:734` 的注释）。
3. SSE 读取侧复用 Redis 连接（连接池或长连接），不要每轮 `blpop` 都建连再关。
4. `/chat` 改 async（内部用 `run_in_threadpool` 包一层即可），把「一个请求占一个线程」变成协程挂起。

### 验收口径

- 50 并发 SSE 持续压测：不出现 `pool_busy` 503、内存曲线收敛（不是单调上升）、p95 延迟有界。
- 人为触发 100 条并发 SSE，第 N+1 条快速拿到明确的排队提示而不是挂起。

---

## 五、PERF-03 · 全链路零缓存

### 现状

- `kg/`、`memory/`、`rag/`、`llm/` 下**没有任何缓存实现**：无 `lru_cache`、无 `TTLCache`、无 `cached_property`、无 Redis 缓存层。（全仓唯一的 "metrics" 命中是 `eval/metrics.py` 的 Ragas 指标，无关。）
- 因此每一句提问都完整走一遍：实体链接（Milvus 检索）→ 图查询（Neo4j）→ LLM 生成。
- 养生场景的热点集中度高：同一批用户反复问同一批菜式。

### 风险

这不仅仅是「慢一点」。LLM 是按次付费的，**重复问题等于重复付这笔钱**。当 COST-01 把明细做出来之后，这一项大概会立刻浮到成本表的第一行。

### 需求（按性价比排序）

1. **embedding 缓存**：归一化后的问句 → 向量，Redis `GET/SET` + TTL（建议 24h）。降本最快、风险最低。
2. **检索结果缓存**：`(实体集合 + intent + evidence_policy) → hash` → 图命中行，分钟级 TTL。
   ⚠️ **key 必须带上 `INCLUDE_PROVISIONAL` 与批次水位**，否则审核通过的新数据会被旧缓存挡住——那会重现 README 里专门解释过的那个坑（「审核通过了却查不到」）。
3. **答案缓存**：只对「无画像、无 session 上下文」的低个性化问句开放，命中率低就下线。可选，看 1、2 之后的成本曲线再定。

### 验收口径

> 本版按**两级缓存（embedding + 检索结果）**验收，答案缓存不做（与需求第 3 条「看成本曲线再定」一致）。
> 所以第二次提问**仍会调用一次模型**，省掉的是 embedding 请求与 Neo4j 查询 —— 原稿写的「LLM 调用计数不增加」在本版做不到，
> 按那句验收这版一定不过。

- 同一句话第二次提问：**不再请求 embedding、不再打 Neo4j**（可用 COST-01 的明细分项 + 图查询计数验证）；
  p95 延迟有可测下降（不以 60% 为硬门槛，因 LLM 仍调用一次）。
- 后台 approve 一个新 bundle 之后，相关问句立即能查到新数据（缓存没有拦住新证据）。
- 忌口不同的两个用户命中不同缓存键（安全项，不是性能项）。

---

## 六、OBS-01 · 只有 tracing，没有 metrics / 告警

### 现状

- **有的**：`observability/tracing.py` 有一层极薄的 trace（只在 `api/chat.py:85` 的 `_invoke()` 里包了整次 `/chat`，`record_result` 写 answer / evidence_counts / graph_hit / elapsed_ms 等 summary），查单个 case 勉强可用。
- **没有的**：全仓无 `/metrics`、无 prometheus 客户端、无任何告警配置。
- **`/health` 的三件套缺口**（`api/health.py:57-73`）：只查 Neo4j / Milvus / PostgreSQL，**不查 Redis**。而 Redis 已是 `/chat/stream` 的硬依赖（`api/chat.py:171-173`：`open_bus()` 返回 None 就直接 503）。
- **`/health` 自身的开销**：`health.py:22-29` 每次调用都 `GraphDatabase.driver(...)` 新建 Neo4j driver 再 `close()`；`health.py:35-44` 每次 `connections.connect()` 再 `disconnect()`。目前 access_log 关掉了（`locations.conf` 里 `access_log off`），但如果将来被负载均衡高频探活，这个成本会显现。

### 风险

现有 tracing 是「事后查案」，不是「事前报警」。三类典型故障当前**不可知**：

| 故障 | 现象 | 谁会发现 |
|---|---|---|
| 模型池全部冷却 + 兜底没额度 | 所有用户收到「暂时答不了」 | 用户先发现 |
| `FDC_API_KEY` 额度打满 | 补录静默不产出，待审队列空着 | 管理员隔天才发现 |
| Redis 挂了 | `/chat/stream` 全 503 但 `/chat` 正常 | **最难发现**：半残状态，`/health` 还是绿的 |

### 需求

1. 先加一个轻量的 `/metrics`（不必上 Prometheus 全家桶），输出这几个就够：
   `chat_total{status}`、`chat_latency`、`graph_hit_ratio`、`degraded_ratio`、`abstain_ratio`、`llm_pool_available`、`sms_sent_total`、`fetch_queue_depth`（最后这个顺带满足 KGD-03）。
2. 配 4 条告警（阿里云云监控免费额度足够）：`/health` 非 200、`graph_hit_ratio` 1h 均值 < 0.3、`llm_pool_available = 0`、短信量日环比 > 5x。
3. **把 Redis 加进 `/health`**——一行的事，但它是当前最可能被漏掉的半残状态。
4. 顺便给 `/health` 的 Neo4j/Milvus 探针加一层 TTL 缓存（比如 10 秒内复用上一次结果），避免被高频探活拖累。

### 验收口径

- 手动 `docker stop db-redis`：2 分钟内收到告警，且 `/health` 变 503 并在 `checks` 里指出是 redis。
- 手动把 `FDC_API_KEY` 换成无效值：`fetch_queue_depth` 停止下降 → 告警。

---

## 七、OBS-02 · 没有请求层的标识

### 现状

- **没有**：全仓无 `X-Request-ID`、无 `X-Trace-Id`、无 `request_id` 中间件（HTTP 语义上的）。
- ⚠️ **同名混淆要注意**：仓库里大量 `request_id` 指的是**未收录食材登记编号**（`kg_ingredient_request.id`，例如 `kg/request.py:94,334`、`scripts/clean_request_noise.py:53`）。两者完全不同，做这块时**请求标识不要用 `request_id` 这个名字**，建议用 `rid`。
- 日志格式目前是 `%(asctime)s %(levelname)s [%(name)s] [pid=%(process)d thread=%(threadName)s] %(message)s`（`utils/logging_config.py:110-113`）——有 pid 和线程名，但一次 `/chat` 会横跨 SSE 工作线程、`profile-extract`、`session-compress` 三个线程（见 PERF-02），单看 threadName 拼不出一次请求的顺序。
- 现有 `observability/tracing.py` 侧有 trace id，但它和进程日志之间没有共同键，无法两边互跳。

### 需求

1. 中间件生成短 `rid`，写入 `contextvars` → 通过 logging filter 自动注入每条日志 → 响应头回传 → SSE 每个事件也带上。
2. 命名统一为 `rid`（避开上面那个同名陷阱）。
3. `rid` 同时作为 OBS-03 span 表的 `trace_id`，实现「日志 ↔ span 表」双向跳查（不再依赖 Langfuse）。

### 验收口径

- 任意抓一次 `/chat`，`grep rid=xxx logs/backend.log` 能拿到本次请求从进入到写库的完整顺序（含两个后台线程的行）。
- 拿这个 `rid` 能在 OBS-03 的 span 表里定位到同一次请求的整棵 span 树。

---

## 八、OBS-03 · 全链路跟踪：多轮对话与「模型/工具」的入参出参耗时不可观察

> 本节是「全链路跟踪」需求的落点，与 OBS-02（rid）、COST-01（span 表）强绑定，三者一起做。编号沿用 OBS 系列，属 A 方案。

### 现状

- 现在唯一的 trace 是 `observability/tracing.py`，但**只有 `api/chat.py:85` 的 `_invoke()` 在调它**，且只包了「整次 `/chat`」这一层：`record_result`（`tracing.py:95`）只在结束时写一个 summary（answer / evidence_counts / graph_hit / elapsed_ms），**没有逐轮、逐调用粒度**。
- `record_span`（`tracing.py:81`，节点级 span）**全仓零调用**——LangGraph 的 12 个节点（`graph/workflow.py:41-52`）内部没有任何 span。
- 「大模型调用」收敛在 `llm/middleware.py` 的 `WrapCallModelMiddleware`（`wrap_model_call`/`awrap_model_call`，`:191`/`:219`）——这是**唯一**能拿到「每次模型调用的 prompt、输出、token、哪个模型、耗时」的天然收口点，但现在它只 `log.info` 一行、把 `pool_model` 写进 `response_metadata`（`:162`），**没有往 trace 写 span**。
- 「工具」在本项目里不是独立的 LangChain Tool 对象，而是节点内直接调函数（`web_answer_node` 搜网页、`link_node` 查图、`graph_query_node` 查 Neo4j 等），这些调用**完全不在任何 trace 里**。

### 需求

1. **span 表复用 COST-01 的 `llm_call_log`**（加 `parent_span_id` + `kind` + `node` 字段后二合一），不自建第二张表。kind 至少区分 `llm` / `node` / `tool`。**父子关系靠一个「当前 span」的 contextvar 串起来**，不能只存字段不赋值。
2. **记账收口在 `llm/middleware.py:158` 的 `on_success`，不是 `wrap_model_call`**：流式主路径（`llm/agent.py` 的 `stream_model`）自己循环、最后调 `plan.on_success`，**不经过 `wrap_model_call`**；只有记在 `on_success` 才能同步与流式都覆盖且只记一笔。`pool_chain` 的两处（食材识别、画像）链对象里看不到 token，账要记在调用点。这一步同时喂给 COST-01 账本。
3. **在工具型节点里写 `tool` span**：至少覆盖 `web_answer_node`（网页搜索，含搜索词/返回条数）、`link_node`/`graph_query_node`（图查询，含 cid/命中数）。收口点已存在，是「往节点函数里加几行」，不用重铺调用链。
4. **后台 trace 详情页**：`/admin` 下按 `rid`（或 session）拉出整棵 span 树，逐层展开看每步入参/出参/耗时。这是**管理端 Vue 页面**（`admin-web/src/views/` 新增 + `router.js` 路由 + `Layout.vue` 菜单），不是只加一个 JSON 接口。
5. **摘除 Langfuse**：本版不引 Langfuse，span 统一存 PG（自建）。`observability/tracing.py` 整个模块摘掉，`api/chat.py:85` 的 `trace()` 调用改为直接写自建 span 表的根节点（`kind='node'`）。

### 验收口径

- 后台点开任意一次 `/chat`，能看到**这一次实际产生的那几行 span**：
  根 `node=chat` 且 **`kind='node'`**；它的 `llm` / `tool` 子 span 的 `parent_span_id` **指向这个根**；
  每步有入参/出参/耗时。
  > ⚠️ 本版**不做 12 个节点逐个 span**，所以树上**不会出现** `guard`/`link`/`assemble` 这些节点。
  > 因此验收**不要求**「父子关系与 `workflow.py` 的边一致」—— 没有节点 span 就没有那些边。
  > 原稿写的固定路径 `… assemble → generate → web_answer → memory_write` **在图上也不存在**：
  > `web_answer` 直接进 `memory_write`（`workflow.py:79`），与 `assemble/generate` 是并列分支。
  > 走网页参考的那一次，应当能看到 `web_answer` 的 `tool` span，但**不要求**出现 `assemble`。
- 一次多轮会话里，连续 N 轮能按 `session_id` 串起来，逐轮展开各自的 span 树。
- 某次 LLM 调用中途换模型（额度耗尽切下一个），span 里能看到两次尝试（第一次失败 + 第二次成功），token/耗时分开记。

---

## 九、KGD-02 · 来源可追溯性不够（有指针，没快照）

### 现状

- **bundle 级出处**（`migrations/006_food_bundle.sql:51`、`kg/fetch.py:195-204`）：

  ```python
  sources = [{
      "type": "database", "title": "USDA FoodData Central",
      "publisher": "U.S. Department of Agriculture",
      "url": f"https://fdc.nal.usda.gov/food-details/{fdc_id}/nutrients",
      "external_id": fdc_id,
      "locator": desc,          # ← 是 USDA 的 description 文本，不是 URL，也不是快照
  }]
  ```

  注意两点：**URL 是按 `fdc_id` 拼出来的**（没有验证过可达），**locator 存的是一段描述文本**。
- **逐项出处**：`kg_food_bundle_item.source_locator`，形如 `fdc:174270#1003`（`006_food_bundle.sql:70` 的注释），它指向「原始响应里的那个条目」，但**原始响应本身没有被保存下来**。
- `kg/verify.py:16` 自己写明了这个降级：「这里的可追溯性退化为『每个数值都能指回原始响应』」。
- 时间信息只有 `created_at` / `reviewed_at`，没有快照、没有校验和、没有复检机制。

### 风险

源站改版或数值修订之后，这条边的出处就自证不了。食疗是会被较真的话题——用户或投诉问一句「你说这个数哪来的」，现在能给的东西只有一条拼出来的 URL 加一段描述文本。

反过来这也是**产品卖点**：「每条结论都能点开看抓取当时的原始资料」几乎没有同类产品做。

### 需求

1. 抓取时把原始响应 JSON 落对象存储（MinIO / OSS），`kg_food_bundle` 加 `snapshot_uri` + `snapshot_sha256` + `fetched_at`。
2. 导入通道（`kg/import_bundle.py`）同理：Excel / 手工录入保留原文件。
3. 季度复检任务：比对 URL 存活 + 数值是否变化（这条依赖 KGD-03 的定时任务基础）。
4. 回答/后台的「来源」展示，优先指向我们自己的快照，而不是外部 URL。

### 验收口径

- 任一条补录出来的边，能在后台一键打开它被抓到时的原始快照，且能校验 `sha256`。
- 人为把某个 `fdc_id` 拼成失效 URL，图里的那条边依然能自证（因为证据在快照里）。

---

## 十、KGD-03 · 补录队列没有驱动器

### 现状

- **全仓没有任何定时机制**：无 crontab、无 systemd timer、无 APScheduler，也无 mention in docs。
- 队列本身是现成的：`kg_fetch_task`（`006_food_bundle.sql:19-34`）已经设计好了 `status`（queued/running/done/failed/rejected）、`attempts`、`last_error`、`budget_day`、`started_at`、`finished_at`、`next_retry_at`。
- 但让它推进的方式只有两种：手工跑 `python -m scripts.run_fetch [--limit|--task-id|--recover|--status]`（`scripts/run_fetch.py`），或管理员在后台逐个点 `/admin/kg/requests/{id}/fetch`（`api/admin.py:725`）。
- 防超支的门已经有了：`FOOD_FETCH_DAILY_BUDGET`（`config.py:329`），`run_fetch --status` 会打印「今日预算已用 N/M」，`fetch.run()` 超限返回 `budget_exhausted`。

### 风险

用户侧的体验是「我登记了黑豆，然后两周没动静」。9.1 那一整套「未收录登记 + `hit_count` 热度排序」设计的正向价值，会败在最后一公里：队列空転而没人跑它。

### 需求

1. 一个 systemd timer（或 crontab）每 10 分钟执行 `run_fetch --limit 5 --recover 15`。优先用 systemd timer，与现有的 `nutri-graph.service` 同一套运维方式。
2. 日志统一进 `logs/`（走 `utils/logging_config`），便于被 OBS-01 的日志/request-id 体系覆盖；不要用 `run_fetch.py:50` 现在那种 `logging.basicConfig`。
3. 把 `fetch_queue_depth`（queued/running 计数）作为指标暴露出来（顺带完成 OBS-01 第 1 条里的最后一个指标）。
4. 失败重试已经有了（`attempts` / `next_retry_at` / `--recover`），定时任务是**补上驱动器**，不是重写队列。

### 验收口径

- 前端登记一个新食材，**无人干预**的情况下 30 分钟内产出 bundle 并出现在待审列表。
- 故意塞 3 个必然失败的任务：系统自动按 `attempts` 重试、留 `last_error`，不阻塞后续任务。
- 今日预算打满后，定时任务不再 claim，且 `fetch_queue_depth` 指标停止下降、能触发告警。

---

## 十一、建议的处理顺序

依赖关系上有一条链：**OBS-01（有指标/告警）和 KGD-03（有定时机制）是其它几项的基础设施**，建议先做：

```
OBS-01 指标与告警  ──┐
                    ├──►  OBS-02(rid) ──► COST-01(span 表) ──► OBS-03(span 埋点+详情页)
KGD-03 定时任务    ──┘         │                                │
                               │                                ▼
                               └──────────────►  COST-02 用户配额（知道谁在花，才知道限多少）
                                                       ▼
                                                PERF-02 并发收敛
                                                       ▼
                                                PERF-03 缓存（有 COST-01 数据才能量化收益）
```

- **OBS-03（全链路跟踪）不单独排期，也不等 needs_v9 全部完成**：它只依赖 OBS-02 的 rid + COST-01 的 span 表，两者都在主干链头部。OBS-03 就是这两条的加宽——span 埋点写在 COST-01 已定位的 `middleware.py` 收口点里，后台详情页挂在 `/admin` 同一体系。因此 OBS-03 紧跟在 COST-01 之后，与 COST-01 同批交付，避免先建纯账本表再回头改 schema。
- **PERF-01（多 worker 并发参数）已移出本需求**：它属于「参数配置」（`--workers` / `--limit-concurrency` / PG 连接池三者绑定的调参），不是需要单独立项开发的需求。等 PERF-02 收敛完成后，真有扩容需求时再作为配置调整单独评估。

OBS-02（request-id）和 KGD-02（来源快照）相对独立，可以并行插空做。

---

## 十二、待确认

1. **月成本红线**：有了这个数字，`COST-02` 的用户日配额、`FOOD_FETCH_DAILY_BUDGET`、`WEB_ANSWER_DAILY_BUDGET`、短信全局熔断才能统一反推，而不是各自拍脑袋。
2. **用户日配额的具体数字**：建议先给一个宽松值（50/天）上线观察一周，再用 COST-01 的明细分位数反推真实阈值。
