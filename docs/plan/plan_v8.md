# 短信回执与问答流式输出（v8.6）

> 来源：[docs/needs/needs_v8.md](../needs/needs_v8.md)
> 评审：[docs/review/review_v8.md](../review/review_v8.md)
> 状态：**v8.6 / 已按第六轮评审修订（2026-10-30）** —— 施工以本文为准。仍不含代码。
>
> 一句话结论：两条都做。短信不反查、走号码认证的 HTTP 回执，扩展现有 `sms_send_log`。问答流式用 `POST /chat/stream`，事件放本机 Redis 列表。不引入 deepagents，不用进程内内存队列。

v8.5 的按句判定顺序保持。这一版补上弱证据和单源补录的免责前缀：它今天写在答案正文最前面，流式方案里没有位置。模型全部抛错时与同步接口一样给固定话术，不发 `error`。

---

## 零、结论摘要

| 项 | 判定 | 施工口径 |
|---|---|---|
| 短信发送记录表 | 做，但不新建第二张表 | 扩展 `sms_send_log`。频控和展示必须看同一张表 |
| 发送后反查阿里云 | 不做 | 号码认证没有按手机号查记录的接口 |
| 订阅回执 | 做 | HTTP 批量推送 `DypnsSmsVerifyReport`。像我们自己的 `out_id` 却没有行才返回 500；样例格式返回 200 |
| 后台短信列表 | 做 | 手机号 + 时间范围，时间倒序，`page` 从 1 起，每页 20。非法号码 **422**。只显示掩码 |
| console 发码 | 会落 `sms_send_log` | 记账在 `api/auth.py` 的 `send_sms` 调用 `record_send()`。`provider=console`，没有运营商回执 |
| 页面流式输出 | 做 | `POST /chat/stream`。检索先给状态，模型按句推正文。同步 `POST /chat` 保留 |
| deepagents | 不依赖 | 本项目是固定 LangGraph 加一次模型调用 |
| 事件队列 | 本机 Redis 列表 | 工作线程 `RPUSH`，SSE `BLPOP`。不用 `asyncio.Queue`，不用 Pub/Sub |
| Redis 放在哪 | 容器，应用无感 | 连 `127.0.0.1:6379`。进程在容器 `db-redis`，数据在宿主机 `/opt/db/redis/data` |

**不做**：不调用 `QuerySendDetails`；不拉阿里云 MNS；不把验证码或短信正文落库；不把 deepagents 加进依赖；不把 Redis 键塞进 `RAGState`；不把 Redis 密码写进仓库；不改画像抽取那条提示词；不在开发机起库或跑 pytest。

---

## 一、需求对照

原文见 [docs/needs/needs_v8.md](../needs/needs_v8.md)。

1. 增加短信发送记录，发送后拿到阿里云侧的送达结果，后台能按手机号和时间查。
2. 前台问答改为流式输出。技术上做得到。不依赖 deepagents。LX_AICoding 只参考「工作线程 + 同一次 POST 的 SSE」，队列换成 Redis 列表。

---

## 二、阿里云现在支持哪一种

本项目的通道是号码认证里的短信认证，不是短信服务。`SMS_PROVIDER=dypns`，发送是 `SendSmsVerifyCode`，核验是 `CheckSmsVerifyCode`（[security/sms.py](../../security/sms.py)、[docs/sms-dypns-setup.md](../sms-dypns-setup.md)）。

| | 短信服务 SMS（`dysmsapi`） | 号码认证 · 短信认证（`dypnsapi`，本项目） |
|---|---|---|
| 发送 | `SendSms` | `SendSmsVerifyCode` |
| 发送后反查 | 有。`QuerySendDetails`，必须带手机号和日期，最近 30 天 | **没有。** OpenAPI 只有发送和核验 |
| 主动通知 | `SmsReport`，MNS 或 HTTP | **`DypnsSmsVerifyReport`，MNS 或 HTTP** |
| 控制台 | 发送记录页 | 短信认证 → 发送记录，明细 180 天。这是控制台，不是 API |

`QuerySendDetails` 查不到号码认证发出的验证码。本方案用 HTTP 批量推送，不消费 MNS。

回执约束（施工照做）：

- 正文是 JSON 数组。字段：`phone_number`、`send_time`、`report_time`、`success`、`err_code`、`err_msg`、`sms_size`、`biz_id`、`out_id`。没有验证码。
- 示例里的手机号是打码的。对账用 `out_id` / `biz_id`，不用回执里的号码。
- 成功必须 **HTTP 200**，正文 `{"code":0,"msg":"接收成功"}`。处理超过 **700ms** 或非 200，会在 1 分钟、5 分钟、10 分钟后再推，共 3 次。
- 不保证幂等。控制台打开后大约 15 分钟才稳定推送。没有签名，也没有固定来源 IP。
- `sms_size` 是字符串（`"1"`）。`report_time` 是无时区的本地时间字符串，按 **Asia/Shanghai** 解析成 `timestamptz`。空串或非法时间不写 `report_at`，不要用 naive datetime 直接入库。

控制台 180 天的发送记录页只做人工兜底。3 次推送都失败的，本版不自动补拉。

---

## 三、短信记录

### 3.1 为什么不新建表

[migrations/001_auth.sql](../../migrations/001_auth.sql) 已有 `sms_send_log`。频控、今日额度、概览里的「今日短信量」都读它。再造一张表，同一条短信会拆成两处。

现在缺的是送达结果：

- `provider_msg_id` 存的是我们的 `out_id`（`login-` 加 12 位十六进制）。阿里云返回的 `BizId` 被丢掉了（[security/sms.py](../../security/sms.py) 只把 `out_id` 放进 `SendResult.msg_id`）。
- `ok` 表示**接口是否受理**，不是手机是否收到。受理成功但运营商失败时阿里云不计费，我们这边 `ok` 仍为 true。这个含义不能改。
- 没有 `phone_mask`。表里只有 `phone_hash`，列表无法展示。

### 3.2 表上补什么

迁移 `025`（当前最新是 `024_food_admin.sql`），列都加在 `sms_send_log` 上：

| 列 | 含义 |
|---|---|
| `phone_mask` | 展示用，如 `138****8000` |
| `biz_id` | `Model.BizId`。console 留空 |
| `delivery_status` | `none` / `pending` / `delivered` / `failed` |
| `err_code` | 回执状态码。成功一般是 `DELIVERED` |
| `err_msg` | `TEXT`。截断在代码里做，收到 200 字为止，不建 `VARCHAR(200)` |
| `sms_size` | 整数，可空。由回执字符串转换，空串和缺字段写 `NULL` |
| `report_at` | 带时区的回执时间 |

状态：

- `none`：console，或接口受理失败。没有运营商回执。
- `pending`：dypns 受理成功，回执未到。
- `delivered` / `failed`：按回执的 `success`。

索引：`biz_id`、`provider_msg_id`。列表用已有的 `(phone_hash, created_at DESC)` 和 `(created_at DESC)`。

历史行：`delivery_status` 写 `none`。`phone_mask` 从 `app_user` 按 `phone_hash` 回填。对不上的留空。注销用户的 `phone_mask` 已被写成空串（[api/auth.py](../../api/auth.py) 注销更新），回填结果也是空，这不是回填失败。

### 3.3 发送时记下对账键

`SendResult` 增加 `biz_id`。dypns 受理成功：`provider_msg_id` 仍是 `out_id`，另存 `BizId`，写入 `phone_mask`，`delivery_status='pending'`。console 成功：`delivery_status='none'`，`biz_id` 空，`provider_msg_id` 为 `console-` 加 8 位十六进制。

`SMS_PROVIDER=console` 也会进这张表。`record_send()` 的函数体在 [security/ratelimit.py](../../security/ratelimit.py)，调用点在 [api/auth.py](../../api/auth.py) 的 `send_sms`：`provider.send()` 返回后无论成败都记一行。图形验证码没过、频控拒绝，发生在 `send()` 之前，不记行。`send()` 自己抛异常也不记行。

### 3.4 回执接口

`POST /auth/sms/report?token=`，不走登录。

1. `SMS_REPORT_TOKEN` 未配置或是空串：启动时打一条 error，回执接口直接 **503**，不跟请求里的 token 做比较，不改任何行。空串和空串用常数时间比较会得到通过。请求侧记一条 warning，只记来源 IP。
2. token 已配置时，与请求参数常数时间比较。不对就 **404**，正文不要写成阿里云的成功格式。这条分支同样只记来源 IP，不记 token。
3. 对账键按下面的顺序，不插新行：
   - `out_id` 符合 `login-` 加 12 位小写十六进制，且能找到行：按 `provider_msg_id` 更新这一行。
   - 符合这个格式但找不到行：先按 `biz_id` 再找一次。对得上就更新那一行。都对不上才 **500**，让阿里云重试。`out_id` 写坏了但 `biz_id` 还在时，不能连重试三次都 500，把回执丢掉。
   - `out_id` 为空或格式不对，但 `biz_id` 对得上已有行：按 `biz_id` 更新这一行。
   - 其它（控制台测试样例、`console-` 前缀、乱码）：**200** 且 `{"code":0,"msg":"接收成功"}`，记一条 warning，不改表。
4. 更新限定一行：`WHERE id = (SELECT id ... ORDER BY id DESC LIMIT 1)`。先按 `provider_msg_id`，没有行再按 `biz_id`。重复推送再更新一次即可。
5. 成功路径只做这一次索引更新，保证落在 700ms 内。日志只记 `biz_id`、`out_id`、`success`。

nginx 用精确路径关掉 access log，避免 `?token=` 进访问日志。`location =` 不继承 `/auth/` 里的任何指令，四条都要写上，只写 `access_log off` 会落到静态文件并 404：

```nginx
location = /auth/sms/report {
    access_log off;
    limit_req zone=nutri_api burst=20 nodelay;
    limit_req_status 429;
    proxy_pass http://nutri_graph_api;
    include /etc/nginx/nutri-graph-proxy.conf;
}
```

控制台地址形如 `http://8.134.253.167/auth/sms/report?token=<SMS_REPORT_TOKEN>`。用「测试」时期望 **200**（样例对不上任何行）。保存后等约 15 分钟。`/auth/` 的限流是每 IP 每分钟 60 次，当前每天约 200 条短信打不满。

`ok` 不随回执改写。概览里的今日短信量仍是接口受理次数。送达看 `delivery_status`。

### 3.5 后台列表

菜单加在 [admin-web/src/views/Layout.vue](../../admin-web/src/views/Layout.vue)，路由加在 [admin-web/src/router.js](../../admin-web/src/router.js)，页面 `SmsLogs.vue`。

`GET /admin/sms`，`require_admin`。分页用 `page`（从 1）和 `total`，每页 20，与食物查询那一页相同，不要抄导入记录的 `limit`/`offset`。

- `phone`：可选。非空时走和发码相同的校验，不合法返回 **422**，`detail.code=invalid_phone`。合法则 `phone_hash` 等值匹配，不要 `LIKE`。响应里带回本次查询的 `phone_mask`，列表为空时能区分「没发过」和「输错一位」。
- `from`、`to`：可选，`created_at` 闭区间。都空则最近 7 天。跨度超过 31 天返回 400。`from` 是 Python 关键字，形参写成 `from_`，并用 `Query(alias="from")`。漏了 alias，前端的 `?from=` 会一直是空。
- 排序：`created_at DESC, id DESC`。

每行：时间、`phone_mask`、用途、通道、接口是否受理、`delivery_status`、`err_code`、`err_msg`、`biz_id`、`provider_msg_id`。不返回 `phone_hash`、验证码、短信正文。

页面状态文案：未回执、等待回执、已送达、送达失败。console 的 `none` 显示「无回执（本地）」。

---

## 四、流式输出

### 4.1 现在为什么是一整包 JSON

[frontend/assets/app.js](../../frontend/assets/app.js) 的 `ask()` 对 `POST /chat` 做 `await r.json()`。后端 [api/chat.py](../../api/chat.py) 同步 `agent.invoke()`。生成在 [graph/nodes.py](../../graph/nodes.py) 的 `generate_node`，经 `invoke_model()` 一次拿完全文。提示词要求 JSON（[graph/boundary.py](../../graph/boundary.py) 第 97、124、156 行）。第 189 行的画像抽取提示词不动。

等待分两段：检索，然后模型。流式只消掉第二段的空白。连接一建立就先推「正在查找」。

nginx 已关 `proxy_buffering`，读超时 120 秒（[deploy/nginx/nutri-graph-proxy.conf](../../deploy/nginx/nutri-graph-proxy.conf)）。`/chat/stream` 落在现有前缀 `location /chat` 上，限流仍是每 IP 每分钟 10 次、突发 3。不必新加 location。上线时看一眼服务器的 `nginx.conf` 没有对 `text/event-stream` 打开 `gzip`（仓库里的站点配置没有 `gzip on`）。响应头加 `Cache-Control: no-cache` 和 `X-Accel-Buffering: no`。

uvicorn 是 `--workers 1 --limit-concurrency 10`（[deploy/systemd/nutri-graph.service](../../deploy/systemd/nutri-graph.service)）。一条流占一个并发槽直到 `done`。当前问答并发低，本版不改这个数。

### 4.2 做得到，但不吐 JSON

模型走 DashScope 兼容接口，`ChatOpenAI.stream()` 能逐段出正文。FastAPI `StreamingResponse` 写 `text/event-stream`。

不要把现有 JSON 原样往外推，用户会先看到 `{"answer":"`。依据、表格、子图在检索结束时就已经在 state 里。`confidence` 和 `gaps` 页面没用，结束时的 `result` 仍带上，历史和测试不丢字段。

固定话术不拆成假逐字：红旗、政治类、未收录、没有证据，各一条 `text_delta`。只有图谱生成和网页参考逐段推模型正文。

### 4.3 不依赖 deepagents，模型池要继续记账

LX 的 token 来自 DeepAgents 的 `stream_events(version="v3")`。本项目不是 Agent 循环。引进 deepagents 等于换掉检索链路。依赖里不加它。

流式不能走 `runner.invoke()`，那会缓冲到结束。也不能裸写 `for spec in candidates(): chat_model(spec).stream()`。候选顺序、额度摘出、冷却、`enable_thinking`、成功失败落盘都在 [llm/middleware.py](../../llm/middleware.py) 的 `_Plan`（`mark_failure` / `mark_success` 写入 `data/llm_pool_state.json`）。少了记账，额度已空的模型会永远排在第一个，每次流式都先撞它一次。额度类错误通常马上返回；连接挂起才会吃满 `LLM_TIMEOUT`（默认 90 秒）。

`_Plan.__init__` 现在收的是 LangChain 的 `ModelRequest`，从 `request.model` 上取温度和 `max_tokens`。`stream_model` 只有 prompt，没有这个对象。先把取参拆成 `params_from(temperature, max_tokens)`，`_Plan` 改为收这份参数。中间件传入从 `ModelRequest` 取出的值，`stream_model` 传入空参数，走 `config` 里的默认温度和 `max_tokens`。

然后在 [llm/agent.py](../../llm/agent.py) 增加 `stream_model(prompt)`：

- 还没有任何一句推给调用方时，模型或网络失败：`on_error`，换下一个模型。
- 已经推过正文之后，模型或网络失败：`on_error` 记一次，**不换模型**，抛错给上层发 `error`。
- 客户端断开：抛内部的 `StreamCancelled`，**不调用** `on_error`。这不是模型故障，记进去会把健康模型送进冷却。
- 正常结束：`on_success`。流式调用显式带 `stream_usage=True`。不带时服务端 chunk 的 `usage` 是 null，记账就是 0。不要把这件事归因成「因为设置了 `base_url`」：调用处的这个参数才是开关，去掉 `base_url` 会把请求打到错误的端点。
- 用量不能取最后一片。线上对 `qwen3.8-max` 的实测是：带 `stream_usage=True` 时，用量在后面某个 chunk 的 `usage_metadata` 里（例如 8 片里的第 7 片），最后一片是空的（`usage_metadata is None`）。DashScope 先发一条 `choices` 为空、带 `usage` 的 SSE，再发 `[DONE]`，LangChain 因此多一个空 chunk。遍历时记下**最后一个非空** `usage_metadata`，用它调用 `on_success`。一个都没有就传 `0, 0` 并记一条 warning。
- 一整轮一个正文字符都没有：按同步路径的空回复处理。先把 `max_tokens` 放大重试一次；仍为空就换下一个模型。池里的 `qwen3.8-2.4t-a95b` 在仅思考名单里，`enable_thinking=False` 传给它会 400，所以根本不会传，它可能只出 reasoning。同步路径有 `_starved`，流式绕开中间件之后这层要自己做。全部试完仍没有正文：用下面这句固定话术，禁止把空字符串交给 `save_round`。
- 所有候选都抛 `LLMUnavailableError`，和「返回了空正文」不是同一条路。同步的 `_complete_from_prompt`（[graph/nodes.py](../../graph/nodes.py)）捕获后返回 200，正文是「当前模型暂时都不可用（额度用尽或全部冷却中），无法生成回答。检索结果已就绪，稍后重试即可；如情况紧急请直接就医。」并且落库。流式同样推这条 `text_delta`，再推 `result` 和 `done`，不发 `error`。两条路用同一段话术。
- `enable_thinking` 保持关闭。思考内容不推到页面。

线上模型池的第一个模型 `deepseek-v4-flash-0731` 免费额度已经耗尽，同步路径已把它标成 `removed`。流式必须走同一套 `on_error`，否则下次额度耗尽时每次提问都会先撞一次 403。

`_complete_from_prompt` 和网页参考的 `_generate` 发现当前线程登记了流式回调就调 `stream_model`，否则仍走 `invoke_model`。同步 `/chat` 和测试不用改调用形状。

### 4.4 Redis：容器里的进程，应用仍连 127.0.0.1

LX 用进程内 `asyncio.Queue`。本项目改成 Redis 列表。当前 systemd 是单 worker，Redis 不是为了多进程才上；用它是为了替代内存队列。进程重启时，浏览器那条 SSE 连接会一起断，不要指望 Redis 把没看完的回答续上。

线上实例是容器 `db-redis`（镜像 `redis:8.0`），不是宿主机上的 `redis-server`。Docker 把 `0.0.0.0:6379` 转进容器。应用连 `127.0.0.1:6379`，和装在宿主机上一样。数据目录在宿主机 `/opt/db/redis/data`（容器内 `/data`）。启动参数里有 `appendonly yes`、`maxmemory 512mb`、`maxmemory-policy allkeys-lru`。这三项是这台实例的配置，不是「放进容器就自动如此」。

由此定四条：

1. `BLPOP` 超时且键还在：只发 `: ping`。已经收到过至少一条事件之后键消失（被 `allkeys-lru` 淘汰或被删）：发 `error` + `done`。第一次 `RPUSH` 之前键本来就不存在，不能把这段当成断流。工作线程进来先推「正在查找」，SSE 再开始等。
2. 列表里只放模型输出片段和状态文案，不放用户原问题，不放画像。AOF 在 `DEL` 之后仍会留着这些命令，直到 Redis 重写 AOF。这是留存面，接受它，不把更多个人内容写进去。
3. `REDIS_PASSWORD` 为空，或连不上：`/chat/stream` 在写出 SSE 头之前返回 **503**。同步 `/chat`、登录、短信不连 Redis。端口对公网开放，空密码不能连。
4. 改掉 [security/ratelimit.py](../../security/ratelimit.py) 第 15–16 行「不引 Redis」的注释。短信频控继续用 `sms_send_log`，这句话只说明频控不改用 Redis。

配置加在 [config.py](../../config.py)：`REDIS_HOST`（默认 `127.0.0.1`）、`REDIS_PORT`（默认 `6379`）、`REDIS_PASSWORD`。密码只放服务器环境变量。依赖加 `redis`。

用列表，不用 Pub/Sub。订阅晚于发布会丢事件。一次请求一个键，`stream_id` 当次随机生成，不复用 `session_id`：

- 列表键 `nutri:chat:{stream_id}`。工作线程用同步客户端 `RPUSH` 一条 JSON：`{"event":"...","data":{...}}`。**每次 `RPUSH` 之后立刻 `EXPIRE 600`**，避免断开之后又推进去的键没有 TTL。
- SSE 侧用 `redis.asyncio` 的 `BLPOP`，超时 15 秒，不阻塞事件循环。
- 取消键 `nutri:chat:{stream_id}:cancel`。客户端断开时 `SET` 它并 `EXPIRE`。工作线程每推一句前看一眼。
- 谁删键看谁是最后一个。正常结束时工作线程**不删**：它先 `RPUSH` `done` 再退出。SSE 收到 `done` 之后才 `DEL`。这时线程已经不会再 `RPUSH`。固定话术路径若由线程自己删，SSE 还在 `BLPOP` 上，列表会连 `result` 一起消失。
- 客户端取消：工作线程不再推正文。若列表里还没有 `done`，`finally` 里先尽力 `RPUSH` 一条 `error` 和 `done`。这两条推进去之后**不要 `DEL`**，仍由 SSE 读到 `done` 再删。马上删会把还没取走的 `error` 清掉。
- 线程在推出任何事件之前就异常退出：同样先尽力推 `error` + `done`。推成功就不删。推不进去才 `DEL`。否则 SSE 看到「还没收到过事件、键也不存在」，会一直 ping，页面没有 `done`。
- SSE 侧的总时长上限按本轮会尝试的模型数计算：`(候选数 + 兜底) × LLM_TIMEOUT + 120` 秒。默认超时 90 秒，四个候选再加兜底，最坏要六七分钟，固定 5 分钟会在还在换模型时先把页面收掉。到点仍没有 `done`，SSE 发 `error` + `done` 并结束。工作线程不因此取消：它若随后生成成功，仍按 §4.6 落库，刷新能看到回答。这是上限被打满时的已知情况。`EXPIRE 600` 仍留着，进程被杀掉时靠它清掉残留键。
- `RPUSH` / `EXPIRE` 抛错（Redis 抖动或容器重启）：吞掉异常，记下「这条流已断」，后面不再推。图继续跑完，并且**照常落库**，除非本来就要因取消或模型中途失败跳过落库。Redis 断了不把整张图打成失败，用户刷新还能看到回答。

这个列表键放在工作线程的 contextvar 里，不写进 [graph/state.py](../../graph/state.py)。`RAGState` 是 `TypedDict`，没声明的键会被 LangGraph 丢掉（`_web_answer`、`_redact` 的注释就是这个坑）。contextvar 要在**工作线程内部**、`graph.invoke()` 之前设置。新线程不会继承请求线程上的 contextvar。

照做的传输约定：同一次 POST 里持续输出 SSE，不先建任务再轮询。任何出口最后都有 `done`。前端用现有的 `api()` 拿 `Response`，再读 `response.body`。`api()` 负责 401 时刷新令牌并重放。不能直接 `fetch('/chat/stream')`，也不能用 `EventSource`（只支持 GET，带不上 `Authorization`）。

不搬：v3 事件、`todo_delta`、工具轨迹、用 checkpoint 当聊天记录。历史仍在 `message` 表。

### 4.5 事件

| 事件 | 何时 | 页面 |
|---|---|---|
| `status` | 工作线程一开始「正在查找」。检索结束、开始生成时再一条「正在组织回答」。网页参考在搜索期间是「正在检索公开资料」 | 改卡片上的短句 |
| `text_delta` | 固定话术的整段，或模型的一整句，或网页参考的头和尾 | 追加到正文节点 |
| `answer_replace` | 已推文字和最终正文不同（政治类整段替换，或句末复核不一致） | 替换正文节点的全部文字 |
| `result` | 落库之后，或确定不落库之后 | 与今天 `/chat` 的 JSON 相同，补表格、子图、依据、来源、`session_id`。`session_id` 用图的返回值，没有则用请求里的那个 |
| `error` | 模型中途失败、Redis 键消失、图抛了不该吞的错 | 正文下面加一行失败说明 |
| `done` | 最后一条 | 解开发送按钮 |

§4.4 的总时长打满时，SSE 已经发过 `error` 和 `done`。工作线程不取消，后面若生成成功仍会落库。页面上是失败，刷新后回答可能已经在会话里。

网页参考的拼接顺序写死，避免先出正文再补免责头。`_wrap`（[kg/webanswer.py](../../kg/webanswer.py)）是「登记提示 + 固定免责头 + 正文 + 来源块 + 尾注」：

`status` → 头（一条 `text_delta`，含登记提示和「未经审核」）→ 模型正文按句 → 来源块和尾注（一条 `text_delta`）→ 若与最终字符串不一致则 `answer_replace` → `result` → `done`。

头和尾是固定文案，含「诊断」一类词，不送进 `rewrite_answer`。

### 4.6 生成、敏感词、落库

接地、文档、网页三处提示词改为纯文本，去掉「请以 JSON 返回」。档位和免责声明的规则不变。

`basis` 和 `confidence` 按路径取，不让模型自报：

| 路径 | `basis` | `confidence` |
|---|---|---|
| 图谱事实 | 送进提示词的 `claim` | 强证据 0.8，弱证据 0.4 |
| 文档转述（图 0 命中、向量高于阈值） | 这些 chunk 的 `doc_id`。结果里没有 `title` | 固定 0.4，并保持 `_degraded` |
| 网页参考 | 空列表。来源继续放在 `web_answer.sources`，那是字典，前台 `basis` 的每一项直接赋给 `textContent` | 不超过 0.25 |
| 无证据、弃答 | 空列表 | 0 |

`gaps` 只在代码能写明的分支里填。`_complete_from_prompt` 现在的参数只有 prompt、免责说明和是否降级，函数里读不到 state。图谱的 `claim` 和文档的 `doc_id` 由 `generate_node` 的对应分支算好，连同 `confidence` 传进去。不要在这个函数里再向模型要一份 JSON。

敏感词按句处理，不按 token 推。判定直接对累计文本调用 [kg/sensitive.py](../../kg/sensitive.py) 的 `rewrite_answer`。不要自己写「`scan()` 有命中就不推」：否定句（「不能治疗」）要保留，`marketing` / `superlative`（「最好」）只记命中、不删句。

弱证据和单源补录的前缀今天就在正文里。[graph/nodes.py](../../graph/nodes.py) 在 `tier == "weak"` 时选 `note`：单源补录用 `PROVISIONAL_DISCLAIMER`，否则用 `WEAK_DISCLAIMER`（[graph/boundary.py](../../graph/boundary.py)）。拼法是 `note + "\n" + 模型正文`，然后才做输出扫描。文档转述和网页参考不走这个 `note`。流式若只推模型正文，页面和会话都会丢掉「未经充分验证」或「单一来源补录」。

`note` 在 `status`「正在组织回答」之后、模型正文之前，单独一条 `text_delta`。它不送进 `rewrite_answer`，和网页参考的头尾一样。比较用的「已推文字」只记模型正文，初值是空串，不要把 `note` 放进去：改写结果不以这段前缀开头，前缀判断会全部落空。

页面和落库的全文是：政治类只有 `POLITICAL_ANSWER`，`note` 去掉（今天把 `note` 和正文拼在一起扫描，政治类也会整段换成这句）。模型正文改写后还有字，且不是疗效兜底：有 `note` 时是 `note + "\n" + 正文`，否则就是正文。模型正文被删光：有 `note` 时只留 `note`，不再追加 `MEDICAL_FALLBACK`（拼在一起扫描时，这句免责本身会被留下，兜底话术不会出现）；没有 `note` 时，生成结束才放 `MEDICAL_FALLBACK`。`answer_replace` 的值用这份全文，不能只放模型正文。

模型片段先攒在工作线程里。凑满一句（以 `。！？；` 或换行结束），或累计已满 80 字，或距上次推送已过 2 秒，就对累计的模型正文跑一次 `rewrite_answer`。判等和判前缀用两边 `rstrip()` 后的副本。推给页面的增量从尚未 `rstrip` 的那份上切，已推文字也存这份。`rewrite_answer` 内部已经 `strip()`，整段末尾的换行不在返回值里，句与句之间的换行还在；再拿裁过的串去切片，会把当时还在末尾的换行切掉，下一句就粘上去。用 [kg/sensitive.py](../../kg/sensitive.py) 里的常量 `POLITICAL_ANSWER`、`MEDICAL_FALLBACK` 判等，不把中文话术写死在比较里。

判定顺序是固定话术、疗效兜底、没有变化、变长、变短或整段改写。不能先做「是不是已推文字的前缀」：空串是任何字符串的前缀，还没推过字时会把 `MEDICAL_FALLBACK` 当成新正文追加出去。后面的合规句就不再是前缀，页面留下兜底话术，会话里却只有那句合规内容。

- 等于 `POLITICAL_ANSWER`：`answer_replace`，已推文字改成这句。模型流继续读完，后面的片段丢掉，结束时照常 `on_success`。不要在流中间抛错，否则一次主动停下会被记成模型失败，usage 也拿不到。
- 等于 `MEDICAL_FALLBACK`：不追加到页面，已推文字记成空串，继续收后面的句子。生成没结束之前不把这句兜底话术展示出来。页面上若已经有模型正文，`answer_replace` 回到只剩 `note`（没有 `note` 时换成空串）。空串必须把文本节点清掉，不能因为空串跳过赋值。这一轮还没有模型正文时，补一条 `status`「正在核对表述」，同一轮只发一次。
- 与已推文字相同：不动。80 字或 2 秒兜底之后，若改写结果没有新增，走这里。
- 以已推文字为前缀，且返回值不是上面两个常量：多出来的模型正文用 `text_delta` 推出去。已推文字为空、返回值是合规句时，也走这里。`note` 已经在页面上，这里不再重复推。
- 已推文字以返回值为前缀，或两边对不上：`answer_replace` 换成上面写的全文（有 `note` 就带上），比较用的已推文字只改成模型正文那一段。80 字或 2 秒先推出去的片段，若整句后来被疗效规则删掉，返回值比已推文字短，走这里撤回。只是「不再多推」会把该删的话留在页面上。

每次推出新字或 `answer_replace` 之后，比较用的已推文字都要改成模型正文那一段。漏了这一步，后面每次前缀判断都还在用旧句子。

生成结束后再对模型正文跑一次 `rewrite_answer`，按上面的全文规则收成最终文字：没有 `note` 且仍是 `MEDICAL_FALLBACK` 时，才把这句兜底放进正文节点。页面最终文字和落库用同一份，不能只存模型正文。扫描抛错时原样返回，记日志，不让这轮 500。

`/chat` 同步接口保留。它只在全文生成结束后改写一次，不存在「中途 fallback 把后面的句子掐掉」。响应形状不变。

落库仍由 `memory_write` 做，但不是无条件。工作线程在 contextvar 里放一个「本轮不落库」标记。两种情况设上它：模型在已经推过正文之后失败；客户端断开（取消键命中）。Redis 写失败**不**设这个标记。`memory_write_node` 见到标记就返回空，不调用 `save_round`。标记和流式回调一样，只在工作线程里设置，不进 `RAGState`。`result` 已经发出之后的断开不再撤销。画像抽取和会话压缩仍是后台线程。

`result` 事件的 JSON 与同步 `/chat` 用同一个函数组装，从 [api/chat.py](../../api/chat.py) 现在的返回体抽出来。`session_id` 写成 `result.get("session_id") or session_id`：不落库时图不会带回新的会话 id，老会话要沿用请求里的那个。两处各写一份，字段会漂。

不要为了流式把图拆成两段请求。工作线程里照常 `graph.invoke()`。

### 4.7 前台

`ask()` 仍通过 `api()` 发 `POST /chat/stream`。发出后保持一张卡片壳：正文是单独的文本节点，表格、子图、依据、来源是后面的占位。`text_delta` 只改文本节点。`answer_replace` 把这个文本节点设成事件里的全文，包括空串；空串是撤回，不是「没有内容所以跳过」。`result` 再填表格、子图、依据、来源、免责声明，并写入 `session_id`。不要每个片段调用一次现在的 `renderAnswer`，那会把整张卡片连子图一起重建。

`done` 或 `error` 之后恢复输入。流没结束时发送按钮保持禁用。

[frontend/index.html](../../frontend/index.html) 里 `app.js` / `styles.css` 的 `?v=` 现在是 `20261029b`，`/assets/` 缓存 7 天。改脚本必须换版本号，否则老页面一周内仍是旧的 `ask()`。

历史回放不走 SSE，仍读会话消息。

---

## 五、接口与配置

| 方法 | 路径 | 谁能调 | 作用 |
|---|---|---|---|
| POST | `/auth/sms/report?token=` | 阿里云，凭 token | 回写送达状态 |
| GET | `/admin/sms` | 管理员 | 短信列表 |
| POST | `/chat` | 与现在相同 | 同步 JSON，形状不变 |
| POST | `/chat/stream` | 与 `/chat` 相同的身份规则 | SSE |

`/chat/stream` 的请求体与 `/chat` 相同。校验失败在写出 SSE 头之前用原来的 HTTP 状态码返回 JSON。开始推流之后的失败只用 `error` + `done`。

环境变量：`SMS_REPORT_TOKEN`、`REDIS_HOST`、`REDIS_PORT`、`REDIS_PASSWORD`。后一个为空则流式接口 503。

---

## 六、迁移 025

新增 [migrations/025_sms_delivery.sql](../../migrations/025_sms_delivery.sql)：

- §3.2 的列。旧行 `phone_mask` 默认空串，`delivery_status` 默认 `none`，`err_msg` 用 `TEXT`。
- `biz_id`、`provider_msg_id` 两个索引。
- 用 `app_user` 回填掩码。注销账号回填完仍是空串。

不新增表。

---

## 七、施工顺序

1. 迁移 025。发送成功时写入掩码、`biz_id`、`pending` 或 `none`。
2. 回执接口：token 未配置返回 503；已配置才比较。`out_id` 对不上时先试 `biz_id`，都没有才 500。单行更新，上海时区，`sms_size` 从字符串转换。nginx 精确路径按 §3.4 写全四条指令。
3. 管理端列表。非法号码 422。分页用 `page`。
4. 提示词改为纯文本。`generate_node` 按路径算好 `basis` 和 `confidence`，传给 `_complete_from_prompt`。网页路径的 `basis` 保持空列表。改掉仍在断言 JSON 字符串的旧用例（`tests/test_retrieval_plan.py` 里喂 `{"answer":"行",...}` 的那条，以及 `tests/test_web_answer.py` 里依赖 `_parse_model` 拆 JSON 的断言）。
5. `config.py` 加 Redis 三项和 `SMS_REPORT_TOKEN`，`requirements.txt` 加 `redis`，改掉频控文件里「不引 Redis」的注释。把 `_Plan` 的参数拆出来，再写 `stream_model`：`stream_usage=True`，取消不记账，零正文按空回复重试。
6. `POST /chat/stream`：先推「正在查找」再等；正常结束由 SSE 在 `done` 之后删键；按 `rewrite_answer` 的返回值按句推送。Redis 用一层薄适配，测试用内存实现，不连真 Redis。
7. 前台改读 SSE，卡片壳不整卡重建，`index.html` 的 `?v=` 换新。

验证在服务器 `8.134.253.167` 上做。不在开发机起库或跑 pytest。

控制台：配好 `SMS_REPORT_TOKEN` 并重启后，用「测试」看 200，再发一条真实验证码。列表应从「等待回执」变成「已送达」或「送达失败」。回执可能要几分钟。

---

## 八、测试与验收

必须有的断言：

- dypns 受理成功带 `biz_id`、`phone_mask`、`delivery_status=pending`，`ok` 仍为 true。受理失败 `ok` 为 false，`delivery_status=none`。console 成功也插入一行，`provider=console`，`delivery_status=none`。频控拒绝不插入。
- `SMS_REPORT_TOKEN` 为空时回执接口返回 503，不改行。token 错误返回 404，不改行，并有一条不含 token 的 warning。`login-` 加 12 位十六进制且对得上时只更新一行；再推一次不增加行。同样格式、`provider_msg_id` 没有、`biz_id` 对得上时按 `biz_id` 更新。两个都对不上返回 500，不插入。样例 `out_id` 返回 200，不改表。
- `sms_size` 为 `"1"` 时入库是整数 1；缺字段为 `NULL`。`report_time` 的 `2017-01-01 00:00:00` 按上海时区入库。
- 管理端不带手机号时只返回时间窗内的行，按时间倒序。带手机号时响应含本次 `phone_mask`。非法号码 422。跨度超过 31 天 400。响应里没有验证码和完整手机号。
- 同步 `/chat` 的 `answer` 是纯文本。图谱路径的 `basis` 来自事实 `claim`。文档路径的 `basis` 是 `doc_id`，`confidence` 为 0.4。网页参考的 `basis` 是空列表，`confidence` 不超过 0.25，来源仍在 `web_answer.sources`。
- 流式适配层：固定话术在 `done` 被取走之前列表还在，取走之后键才删。假模型逐段返回时，页面最终文字等于全文的 `rewrite_answer` 结果，不只断「某一句有没有推出去」。「不能治疗」这句仍出现；「最好」不导致整句消失。疗效句不出现。句与句之间的换行还在。前两句被疗效规则删掉、第三句是合规营养数据时，页面最终只有第三句，中途不出现 `MEDICAL_FALLBACK`。先按 80 字推出的片段，若整句后来被删，页面上这段被 `answer_replace` 撤回；撤回成空串时文本节点是空的。弱证据档位下，页面最终文字和 `save_round` 的 `answer` 都以 `WEAK_DISCLAIMER` 开头；模型正文被删光时只剩这句前缀，不追加疗效兜底。单源补录同理，前缀是 `PROVISIONAL_DISCLAIMER`。政治类的最终文字是 `POLITICAL_ANSWER`，不含这段前缀，流仍读完并 `on_success`。生成结束时没有 `note`、正文仍为空，才用 `MEDICAL_FALLBACK`。假模型只有 reasoning、正文为空时，页面有「当前模型暂时都不可用」这段话术，`save_round` 的 `answer` 非空。所有候选都抛 `LLMUnavailableError` 时同样是这段话术加 `result` 和 `done`，没有 `error`。第一个片段之前失败会换模型并 `mark_failure`。已经推过之后的模型失败有 `error` 和 `done`，且 `save_round` 不被调用。取消不调用 `mark_failure`。前几句都被删、页面还没有模型正文时，有一条 `status`「正在核对表述」。
- 用量：假流的最后一片 `usage_metadata` 为空、倒数第二片有 `completion_tokens` 时，记账用倒数第二片，结果大于 0。全程没有 usage 时记 0，并有一条 warning。
- Redis 适配层在写出 SSE 头之前就连不上：`/chat/stream` 返回 503，同步 `/chat` 仍返回 200。`RPUSH` 之后键上有 TTL。正常结束后键还在，直到消费方读到 `done`。线程在任何 `RPUSH` 之前失败时，列表里仍有 `error` 和 `done`。流中途 `RPUSH` 失败不阻止 `save_round`。SSE 上限是 `(候选数 + 兜底) × LLM_TIMEOUT + 120` 秒，不是固定 5 分钟。不落库时 `result.session_id` 等于请求里的 `session_id`。

服务器上再看一遍：后台用真实发过验证码的手机号加上今天能查到记录；前台正文按句出现，结束后表格和子图仍在；刷新会话，内容与流结束时一致。控制台测试按钮得到 200。

---

## 九、已拍板

1. 短信记录扩展 `sms_send_log`，不新建表，不改 `ok` 的含义。
2. 送达结果只来自号码认证的 HTTP 回执。`SMS_REPORT_TOKEN` 为空时接口 503。样例 `out_id` 返回 200。我们自己的 `out_id` 找不到行时先试 `biz_id`，都没有才 500。不调用 `QuerySendDetails`，不消费 MNS。
3. 列表展示掩码，查询用手机号哈希等值匹配。非法号码与发码接口一样返回 422。
4. `SMS_PROVIDER=console` 的下发同样写入 `sms_send_log`，`delivery_status=none`。
5. 流式用 `POST /chat/stream`。同步 `/chat` 保留。事件用 Redis 列表。应用连接 `127.0.0.1:6379`。
6. 不引入 deepagents。`_Plan` 先改成收温度和 `max_tokens`，流式再复用它的记账。调用带 `stream_usage=True`，用量取最后一个非空的 `usage_metadata`。客户端取消不记模型故障。
7. 正常结束由 SSE 在收到 `done` 之后删 Redis 列表。异常退出先尽力推 `error` 和 `done`，推成功就不删。`RPUSH` 失败不阻止落库。
8. 推到页面上的是纯文本。每次改写后的顺序是：政治类终局、疗效兜底先不展示、没有变化、变长才追加、变短就撤回。空串不能拿来做前缀判断。弱证据和单源补录的 `note` 先推、不进扫描，比较用的已推文字不含它；政治类最终文字不含它，正文被删光时只留它。政治类停下之后模型流继续读完并记成功。模型全部不可用与零正文用同一段话术，不发 `error`。图谱依据用 `claim`，文档依据用 `doc_id`，网页依据保持空列表。`result` 与同步 `/chat` 共用一个组装函数。
