## 一、需求简述

```
企业知识库问答页（kb-web）的对齐项，参照主站食疗问答页（frontend/）：
1. 一次会话支持多轮对话：同一个会话里连续追问，能理解"它""上文提到的那个制度"等指代；
2. 左侧能展示历史会话：像主站一样有侧栏，列出历史会话（标题 + 最后活跃时间），点一下能打开旧会话继续聊；另有"新对话"入口；
3. 多轮对话的上下文优化跟主站保持一致：最近 N 轮原文 + 更早轮次的滚动摘要，用同一套 ShortTermMemory 机制，不另起炉灶。
```

## 二、需求详述

> 性质：这是**需求文档**（现状差距 + 逐条需求 + 与主站的关系 + 待拍板点），不是实施方案。方案留给 plan_v11。
> 上游：needs_v10（企业知识库图文问答线）已定「问答走独立前台 kb-web」「前端 Vue、不用 Gradio」「录入审核走后台 admin-web」。
> 本轮是在 v10 已施工的基础上，把 kb-web 问答页的**交互能力**补齐到与主站问答页同一档，不推翻 v10。

### 2.0 现状盘点（为什么这轮成本不高）

v10 施工时，企业知识库的后端**已经把多轮对话和上下文优化做了一大半**，只是前端没跟上。先看清已有什么、缺什么：

| 能力 | 主站（frontend + /chat） | 企业知识库现状（kb-web + /kb/ask） | 差距 |
|---|---|---|---|
| 多轮对话（同会话追问） | `/chat` 带 `session_id`，`load_context` 拼上下文 | `/kb/ask` 带 `client_id`/`session_id`，`kb/workflow.py` 的 `_generate` 已把 `history` 拼进 prompt | **已对齐**，无需大改 |
| 上下文优化（滚动摘要） | `ShortTermMemory`：最近 N 轮原文 + 水位线摘要 + 未盖住更早原文 | `kb/memory.py` 复用同一个 `ShortTermMemory`，user_id 加 `kb:` 前缀隔离，摘要 prompt 换成 kb 三段式 | **机制已对齐**，摘要口径是 kb 自己的 |
| 左侧历史会话列表 | `GET /sessions` + `/recent-questions`，前端 `#session-list`/`#ask-list`/`#recent-list` 三栏 | **无列表接口**（`kb/memory.py` 只封装了单会话 messages）；前端只有「新对话」按钮，历史靠 localStorage 单个 session_id 恢复 | **缺失**，本轮核心 |
| 「最近问过」快捷入口 | `/recent-questions`，点一下再问一句 | 无 | 缺失（可选，见 2.5） |

**结论**：本轮不是从零做多轮对话，而是「把 v10 已经埋好的后端能力在前端补一个历史会话侧栏，并补一个列表接口」，顺带对齐「最近问过」。

### 2.1 逐条需求

| 编号 | 需求 | 关键落点 |
|---|---|---|
| KBQ-01 | 一次会话支持多轮对话 | 保持现状：`/kb/ask` 带 `session_id`，追问能理解指代。本轮**不新增**，只做回归验收 |
| KBQ-02 | 左侧展示历史会话 | 新增 `GET /kb/sessions` 列表接口 + kb-web 前端左侧栏（会话列表 + 新对话入口） |
| KBQ-03 | 多轮上下文优化与主站一致 | 复用 `ShortTermMemory` 的「最近 N 轮 + 滚动摘要」；摘要 prompt 用 kb 自己的三段式，不改主站四段式 |
| KBQ-04 | 打开旧会话继续聊 | 前端点历史会话 → 拉该会话 messages → 恢复对话，`session_id` 续传 |
| KBQ-05 | 「最近问过」快捷入口（可选） | 对齐主站 `/recent-questions`，kb 侧暴露 `GET /kb/recent`（见 2.5 拍板） |
| KBQ-06 | 流式回答 | 对齐主站 `/chat/stream`：新增 `POST /kb/ask/stream`，答案逐字推到前端，命中块在结束时一次性返回 |

### 2.2 与主站的关系（哪些照搬、哪些不照搬）

**照搬（同一套机制，不重复造轮子）**：

| 主站资产 | kb 怎么用 |
|---|---|
| `ShortTermMemory`（session 表 + message 表 + `load_context`/`save_round`/`list_sessions`/`list_messages`/`recent_questions`/`compress_if_needed`） | `kb/memory.py` 已经复用它，user_id 前缀 `kb:` 隔离，主站侧栏看不到 kb 会话、kb 也看不到主站会话 |
| 滚动摘要机制（最近 N 轮 + 水位线 `summary_until_msg_id` + 合并式摘要） | 直接用，不改 `short_term.py` |
| 前端侧栏交互范式（会话列表 / 打开旧会话 / 新对话 / 最近问过） | kb-web 照主站 `app.js` 的 `loadHistory`/`openSession`/`startNewChat` 逻辑重写一份 Vue 版 |

**不照搬（领域差异，保持 kb 独立）**：

| 主站 | kb 不照搬的原因 |
|---|---|
| 四段式摘要 prompt（含「用户约束：过敏/忌口/症状/人群」） | kb 场景没有健康约束，`kb/memory.py` 已有三段式（查的制度/系统、回答要点、问题清单），保留 |
| 流式回答（`/chat/stream` + SSE + Redis bus + `stream_model`） | **照搬**：kb 新增 `/kb/ask/stream`，复用 `chatbus`/`stream_model`/`spawn`，但**不复用 `Live`**（`Live` 绑食疗敏感改写，见 KBQ-06） |
| 登录闸门（手机号验证码）+ 敏感信息授权弹层 | kb 是面向企业的演示问答，不登录、用 `client_id` 标识匿名会话（v10 已定），不引入手机号登录 |
| 图谱探索抽屉、证据分层、`facts`/`subgraph` 表格 | 食疗领域特化，kb 命中块直接展示 `hits`，不引入 |

### 2.3 各条展开

**KBQ-01 多轮对话（回归，不新增）。** 现状 `api/kb.py` 的 `_session()` 已做：带 `client_id` 且 `session_id` 为空则 `open_session` 新开会话，带 `session_id` 则校验归属（`owns`）后沿用；`kb/workflow.py` 的 `_generate` 把 `history` 拼成「近期对话（只用于理解指代，不能当成文档依据）」块放进 prompt。**本轮只验收这段不回归**：同一会话里先问「XX 制度里考勤怎么算」，再问「那加班呢」，第二个问题能借助上文理解指代。不新增代码。

**KBQ-02 左侧历史会话列表（本轮核心）。** 后端补 `GET /kb/sessions?client_id=...`，返回 `client_id` 名下的会话列表（复用 `ShortTermMemory.list_sessions`，字段与主站一致：`id`/`title`/`started_at`/`last_at`/`message_count`/`summary`/`round_count`）。前端 kb-web 加一个左侧栏：
- 顶部「新对话」按钮（`fresh()` 清空 localStorage 的 session key，不落库删会话）；
- 「历史会话」列表：按 `last_at` 倒序，标题取首问前若干字，点击 `openSession(id)` 拉 messages 恢复；
- （可选）「最近问过」：见 KBQ-05。

**KBQ-03 上下文优化对齐。** 复用 `ShortTermMemory.load_context` 的三段拼装（最近 N 轮原文 + 水位线摘要 + 未盖住更早原文）和 `compress_if_needed` 滚动摘要。kb 侧已经通过 `kb/memory.py` 的 `_defer_compress` 在 `remember` 后后台触发压缩，摘要 prompt 是 kb 三段式。**本轮不改 `short_term.py` 本体**，只确认 `kb` 这条链路完整：`remember` → `save_round` → `_defer_compress` → `compress_if_needed`（用 `_kb_summarize` 当 summarize_fn）。若发现压缩未触发（例如 `MEMORY_SUMMARY_ENABLED` 未开），在 plan 里对齐配置口径。

**KBQ-04 打开旧会话继续聊。** 前端 `openSession(id)`：拉 `GET /kb/sessions/{id}?client_id=...`（已存在），把 messages 回填到线程，`localStorage` 写入该 `session_id`，后续 `/kb/ask/stream`（流式）续传。与主站 `openSession` 一致，回放时命中块（含图片）照旧渲染，不做流式重放（历史消息一次性渲染）。

**KBQ-05 「最近问过」（拍板项）。** 主站有 `/recent-questions`（点一下是「再问一句」，不是打开旧会话）。kb 要不要对齐，见 2.5。

**KBQ-06 流式回答。** 对齐主站 `/chat/stream`：新增 `POST /kb/ask/stream`，复用主站现成的流式设施——`chatbus`（Redis bus + `iter_sse`）、`llm/agent.py` 的 `stream_model`、`utils.spawn` + 并发信号量、`llm/streamctx.py` 的 `bind`。**不复用 `graph/live.py` 的 `Live`**：`Live.feed → _rewrite` 无条件调 `kg.sensitive.rewrite_answer`（不看 `SENSITIVE_OUTPUT_SCAN`），会把制度回答里的「治疗/诊断」删句、删光换食材话术；kb 制度/方案文档里这些词很常见。所以 kb 自建一个只按句推送、不调 `rewrite_answer` 的推送对象（分句条件同 `Live.feed`）。改动在两处：`kb/workflow.py` 的 `_generate` 加流式分支（`hook()` 有目标时调 `stream_model`，返回值当答案；否则仍 `invoke_model`），`api/kb.py` 加流式端点。答案逐字推到前端，`hits` 在结束时经 `result` 事件一次性返回（图片块 `image_url` 照旧）。**流式成功后要落库**（`memory.remember`，否则下一轮指代/历史会话/回放全断），取消时不落库。图片提问时 `question` 为空，流式 label 与 span 的 input 用 `image_name`。**非流式 `/kb/ask` 保留**（后台评估 `eval/kb_run.py`、测试、命令行继续用它），两个端点共用同一个 `run_ask`，只是流式把生成环节的模型调用走 `stream_model`。前置条件：服务器 `.env` 配好 `REDIS_HOST/REDIS_PORT/REDIS_PASSWORD`（项目已起 `db-redis`），否则 `open_bus()` 返回 None、端点回 503。

### 2.4 明确不做（范围收敛）

- **登录/账号体系**：kb 继续用 `client_id` 匿名会话（localStorage 里一个 UUID），不引入手机号登录、不接入主站账号。
- **会话删除/重命名**：主站侧栏也不做删除（只有列表+打开+新对话），kb 对齐即可，不做删除会话。
- **跨租户历史**：历史会话不按租户过滤——同一 `client_id` 的所有会话（不分租户）都列在侧栏。会话本身不绑定租户（租户是每轮提问时的入参）。若要按租户隔离历史，是另一个需求。
- **摘要 prompt 四段式**：kb 保留自己的三段式，不硬套主站的「过敏/忌口」段。

### 2.5 待拍板问题

1. **「最近问过」要不要做（KBQ-05）**：主站侧栏有三栏（历史会话 / 本轮问过 / 最近问过）。kb 侧栏建议至少做「历史会话」一栏（本轮硬需求）；「最近问过」可选。**建议：做**（复用 `recent_questions`，成本极低，且演示「问过什么」时更完整）。「本轮问过」（`ask-list`）是主站会话内上翻的快捷区，kb 可以不单独做（kb 文本框已有上翻能力不强，但非硬需求）。
2. **历史会话的「标题」**：主站 `save_round` 里 `title` 取首问前 40 字，kb 沿用（同一套 `ShortTermMemory.save_round`，无需改）。确认不需要为 kb 单独改标题口径。
3. **会话列表接口的鉴权口径**：`GET /kb/sessions` 用 `client_id` 查询参数（与现有 `/kb/sessions/{id}` 一致），**不做登录鉴权**，靠 `client_id` 的随机性隔离（`^[A-Za-z0-9_-]{8,64}$`）。这与主站 `/sessions`（走登录态）不同，是 kb「不登录」定位下的既有取舍，本轮维持。
4. **流式与非流式并存**：`/kb/ask`（非流式）保留给评估/测试/命令行，`/kb/ask/stream`（流式）给前端。两个端点共用 `run_ask`，只差生成环节是否走 `stream_model`。默认前端走流式，非流式不停用。

## 三、需求评审

> 第一轮。对照第一节、第二节。未改代码。日期：2026-11-02。
> **与第二节冲突时，以本节为准。**

### 3.1 结论

需求合理，且**成本远低于"从零做多轮对话"**：v10 已经复用 `ShortTermMemory` 把后端的多轮对话 + 上下文优化做完了，本轮真正的缺口是「左侧历史会话列表」的前端 + 一个列表接口，加可选的「最近问过」，再加「流式回答」（复用主站现成流式设施，改动集中在生成节点 + 一个端点 + 前端 SSE）。

动笔前先把四个口径钉死，避免施工跑偏：

1. **不碰 `short_term.py` 本体**。主站和 kb 共用同一个 `ShortTermMemory`，改它会影响食疗线。kb 侧的差异（三段式摘要 prompt、`kb:` user_id 前缀）已经封装在 `kb/memory.py`，本轮只在 `kb/memory.py` 和 `api/kb.py` 里补列表/最近问过两个薄封装，不动核心表逻辑。
2. **会话列表按 `client_id`，不按租户**。会话不绑定租户（租户是每轮提问入参），历史侧栏列出该 `client_id` 名下所有会话，不分租户。不要在 `kb_document` 或 session 表上加租户维度。
3. **流式复用主站设施，但自建推送对象**。`chatbus`/`stream_model`/`spawn`/`bind` 复用；**不复用 `Live`**（绑食疗敏感改写）。kb 自建只按句推送、不调 `rewrite_answer` 的对象。流式成功后要 `memory.remember`，取消不落库。非流式 `/kb/ask` 保留，两个端点共用 `run_ask`。

### 3.2 需求覆盖

| 简述 | 详述落点 | 判定 |
|---|---|---|
| 1. 一次会话多轮对话 | KBQ-01（回归验收，不新增） | 成立，已实现 |
| 2. 左侧历史会话 | KBQ-02（列表接口 + 前端侧栏）、KBQ-04（打开旧会话） | 成立，本轮核心 |
| 3. 上下文优化对齐 | KBQ-03（复用 ShortTermMemory 滚动摘要） | 成立，已复用，补齐触发链路确认 |
| （新增）流式回答 | KBQ-06（`/kb/ask/stream`，复用主站流式设施） | 成立，见 2.3 |

### 3.3 计划按这个口径写

- 后端只加两个薄接口：`GET /kb/sessions?client_id=`（`list_sessions`）、`GET /kb/recent?client_id=`（`recent_questions`，若拍板做 KBQ-05）。都在 `api/kb.py`，走 `kb/memory.py` 新增的两个封装函数。
- 流式加一个端点 `POST /kb/ask/stream`，复用 `chatbus`/`stream_model`/`spawn`/`bind`，**自建推送对象（不用 `Live`）**，`kb/workflow.py` 的 `_generate` 加流式分支，非流式 `/kb/ask` 保留。流式成功后落库、取消不落库。
- 前端 kb-web 加左侧栏：新对话 + 历史会话列表 + （可选）最近问过。逻辑对齐主站 `app.js` 的 `loadHistory`/`openSession`/`startNewChat`，改成 Vue 3 组合式写法。流式用 `readSse` 逐字渲染。
- `short_term.py`、`kb/workflow.py` 的检索逻辑、`kb/memory.py` 的既有摘要/上下文逻辑不改；只确认 `MEMORY_ENABLED`/`MEMORY_SUMMARY_ENABLED` 在 kb 链路下正确生效。
- 登录、会话删除、按租户隔离历史，均不进本期。

### 3.4 范围

| 项 | 本期 |
|---|---|
| 多轮对话回归验收 | 验收 |
| 左侧历史会话列表（列表接口 + 侧栏 + 打开旧会话） | 验收 |
| 上下文优化对齐（复用 ShortTermMemory，确认压缩触发） | 验收 |
| 流式回答（`/kb/ask/stream`） | 验收 |
| 「最近问过」快捷入口 | 做（按 2.5 拍板，默认做） |
| 登录、会话删除/重命名、按租户隔离历史、摘要四段式 | 不做 |

### 3.5 照第二节写

- 后端复用 `ShortTermMemory`，只在 `kb/memory.py` 加 `list_sessions`/`recent_questions` 两个薄封装，`api/kb.py` 加两个 `GET` 接口，不动 `short_term.py`。
- kb 会话用 `kb:` 前缀的 user_id，与主站会话物理隔离（同一张 session/message 表，前缀区分）。
- kb 摘要 prompt 保持三段式，不套主站四段式的「过敏/忌口」。
- 流式复用主站 `chatbus`/`stream_model`/`spawn`/`bind`，**自建推送对象（不用 `Live`）**，改 `_generate` 加流式分支 + 加 `/kb/ask/stream`，非流式 `/kb/ask` 保留。流式成功后落库、取消不落库。
- 前端 kb-web 加左侧栏，交互对齐主站，流式用 SSE 逐字渲染；不做登录。
