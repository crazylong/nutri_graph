# needs_v3 需求分析与执行计划（v3）

> 来源：`docs/needs/needs_v3.md`（5 条需求）
> 分析依据：逐条对照仓库实际代码（下文均给出 `文件:行号`）
> 状态：待评审
> 说明：本文档只做分析与计划，**不含代码实现**。

---

## 零、结论摘要

| # | 需求 | 判定 | 一句话结论 | 工作量 |
|---|---|---|---|---|
| 1 | 录入/导入加敏感词过滤（+ 政治类问句的固定话术出口） | ✅ 合理，但**维度没拆开** | 「功效」与「治疗」不是同一性质；**只有 political 在输入侧整句处理**，疗效类词出现在问句里不是"待收录食材"的问题，交给 `register()` 的 block 与输出扫描 | 3–3.5d |
| 2 | 入库前评审 + 敏感词再审 | ✅ 合理 | 评审链路已闭环，缺的是**审核侧的可见性**和**后端硬闸**，不是"再评一次" | 2d |
| 3 | PostgreSQL 接 SQLAlchemy 连接池 | ✅ 合理，但**别顺手 ORM 化** | 池该上；transaction 语义会反转，这是最容易静默丢数据的地方 | 1.5d |
| 4 | 多轮对话压缩（保留最近 3 轮） | ✅ 合理，且现状是**死代码** | 4.4 的答案：**没有**自动压缩。现有半成品方向还是反的 | 2.5–3d |
| 5 | 会话内快速检索问过的问题 | ⚠️ 这只是个议题，不是需求 | 需先定策略；建议 A+D 先上，语义检索明确不做 | 1–1.5d |

合计 **11–12 人日**，建议拆 3 次发布（见第六节）。裁剪项见第六节（输出扫描 0.5d、`?q=` 0.3d）。

> 本文已按 `docs/review/review_v3.md` 修订（2026-10-29）。修订要点见 §七"评审修订记录"：
> 输入侧只处理 political；命中边接 `memory_write` 而非 `END`；补 Langfuse / 画像抽取两处原词出口；
> 批准口从 2 条补到 3 条；连接池清单补全并改为"收小并发"；摘要改为合并写回 + 三段读取；
> 删掉空适配层、自动升档脚本、`020` 索引。

---

## 一、需求合理性分析

### 1.1 需求 1：录入/导入的敏感词过滤

**方向必须做（合规刚需），但需求把三种不同性质的东西并列了，照字面实现会误杀。**

`docs/合规手册-养生了么上架.md` 第〇节已把「治疗 / 治愈 / 根治 / 处方 / 诊断 / 疗效」列为红线，所以这不是"要不要做"，而是"怎么做得准"。

现状证据：**没有词表、没有扫描函数、没有一处入口校验**。（`敏感` 二字在仓库里出现过，但都在 `security/crypto.py:9` 的"敏感个人信息"和前端文案里，与内容合规无关。）`kg/importer.py:619 validate_row()` 只有三类校验——像不像食材、数值是否超上限、单位是否合理——完全没有内容合规维度。

**四个必须先解决的问题：**

1. **「功效」不能和「治疗」同级。**
   图谱里食材的描述本来就以"功效"为语义单元（`kg/promote.py:320` 写 `summary = f"{name}（{category}）"`，`kg/schema.py:84` 注释"≤40 字摘要，进上下文"）。中医食疗语料的正常表述是"清热解暑、健脾祛湿"，一旦全局屏蔽"功效"二字，等于把语料主干砍掉。
   → 拆成两类字段：
   - **名称类**（名称 / 别名 / 英文名）：出现「功效 / 治疗 / 根治 / 销售」等 → **它根本不是食材名**，直接判非法。这里无需区分性质。
   - **描述类**（备注 / 来源 / source_desc / claim）：只拦**疗效断言**（治疗、治愈、根治、处方、诊断、疗效、药到病除、降三高、抗癌）。"功效"二字本身**放行**。

2. **「毛爷爷」和「销售」触发的是不同动作。**
   - 政治类：任何字段命中即阻断，**且不落审核队列**（不进库就不留传播面）。
   - 营销导流类（销售 / 代理 / 微商 / 加微信 / 电话 / 包邮 / 下单 / 私聊）：名称命中阻断；描述命中标 `review`，审核时展示给人看。

3. **要做归一化，但能复用多少要看清。**
   全角/半角、大小写、零宽字符、中间插点（治·疗）——这些 `kg/normalize.py::normalize_text` 之外再补一遍"去非汉字"即可。
   **简繁折叠不能指望它**：`normalize_text`（`kg/normalize.py:56`）只有 NFKC + 去空白，注释写明繁简依赖 opencc，而 opencc 不在 `requirements.txt` 里、函数里也没有这一步 → 在 `kg/sensitive.py` 里单写。
   **拼音变体（zhiliao）本轮不做**：匹配管线是"去符号 + 子串匹配"，不会把拼音还原成汉字。要做只能把变体当独立词条写进词表，不是归一化能解决的。

4. **词表必须可运营，不能硬编码。**
   合规口径会变，写死在 `.py` 里意味着改词要发版。放 DB 表 + 代码内置兜底（DB 挂了不能失去防护）。

**必须全覆盖的入口（漏一个等于没做）：**

| 入口 | 位置 | 现在的行为 |
|---|---|---|
| Excel 预览 | `api/food_import.py:182` → `importer.validate_table` | 逐行校验，不落库 |
| Excel 提交 | `api/food_import.py:301` → `import_bundle.stage_bundles` | 写 bundle（pending_review） |
| 手工录入 | `api/food_import.py:386` → `importer.validate_row` | 同上 |
| 离线成分表脚本 | `scripts/import_fcd.py` → `importer.validate_row` | 批量入队 |
| 对话登记未收录食材 | `kg/request.py:184 register()` | 写 `kg_ingredient_request` |
| USDA 抓取落库 | `kg/fetch.py:181 _persist()` | 写 bundle |
| 管理端手工登记 | `api/admin.py:368 POST /kg/requests` | 写 request |
| 关系候选 CLI | `kg/review.py` approve | 直接 UPDATE 成 approved，**绕过所有 API** |

→ 实现点放在 `importer.validate_row`（覆盖 4 个批量入口，一处收口）+ `kg/request.py::register` + `kg/fetch.py` 落库前；`kg/review.py` 与 `promote.approve_bundle` 再加一道闸（见需求 2）。

---

### 1.2 需求 2：入库前评审 + 敏感词评审

**合理，且成本很低——评审链路本来就闭环了，缺的不是"评审"，是"看得见"和"拦得住"。**

现有链路：`bundle(pending_review)` → `POST /admin/kg/bundles/{id}/approve`（`api/admin.py:215`）→ `kg.promote.approve_bundle`（`kg/promote.py:236`）→ `kg.merge`（唯一写图入口）。

**真正的三个缺口：**

1. **审核员看不见敏感词。** `kg/promote.py:84 get_bundle()` 只返回 `kg_food_bundle_item` 的原始字段值，没有任何合规标记；前端 `Bundles.vue` / `BundleDetail.vue` 只有"通过/驳回"两个动作。
2. **后端没人拦。** `promote.approve_bundle` 不做内容校验；`kg/review.py` 的 CLI 更是直接 `UPDATE ... status='approved'`。只在入口过滤 = 防不住"入口没拦住的存量数据被批准"和"审核员手滑/被社工"。
3. **没有审计。** 事后无法回答"这条数据当时为什么被放过"。

**需求 1 和 2 不是重复，是刻意双保险**，但两者职责必须分工：

> 入口是**效率闸**（早失败、省人工）；审核是**责任闸**（人签字）。
> 因此：入口命中 `block` 级 → 直接拒，不给入队；审核命中 `review` 级 → 展示 + 需人工确认；审核命中 `block` 级 → **硬性禁止**（后端拒绝，不是前端灰按钮）。

---

### 1.3 需求 3：PostgreSQL 接 SQLAlchemy 连接池

**合理，收益真实。但不要顺手把 SQL 风格改成 ORM。**

现状：**20 个运行时模块**（`psycopg.connect` 在 `tests/`、`scripts/` 里另有分布，不计），每处都是"新开一条 TCP + SCRAM 认证 → 用完关闭"：

```
memory/       short_term.py   long_term.py   anonymous_merge.py
security/     tokens.py       ratelimit.py   captcha.py         sms.py
api/          auth.py         deps.py        food_import.py     admin.py
              admin_import.py                health.py（保持直连，不替换）
kg/           fetch.py        merge.py       import_bundle.py   normalize.py
              request.py      promote.py     review.py
```

> 前一版清单漏了 `api/auth.py`、`kg/fetch.py`、`kg/import_bundle.py`、`kg/normalize.py`、`api/admin_import.py` —— 前两个正是"登录/发码"和"抓取落库"这两条最需要池化的路径，漏了就等于主要收益没拿到。
> **替换范围 = 20 − `health` = 19 个业务模块**（`health` 探活保持短超时直连：池耗尽时健康检查最不该排队）。

一次 `/chat` 至少 2–3 条连接（鉴权 `load_user` + 画像 + 短期记忆），登录/发码路径 4–5 条（频控查、频控写、验证码写、用户查）。PG 在容器内约 1–3ms/次握手，池化后这部分直接消失。

**三个必须说清的点：**

1. **`requirements.txt` 里已经有 `sqlalchemy>=2.0.0`，但全仓零引用。** 所以这不是"加依赖"，是第一次真正用它。既然只用池，就只用在池上，SQL 一行不改。

2. **⚠️ 事务语义会反转——这是最危险的一点。**
   现在 `with psycopg.connect(...) as conn:` 的退出语义是**提交**（psycopg3 的 connection context manager 就是事务边界）。
   换成池之后如果写成 `with engine.connect() as conn:`，退出语义变成**回滚**。
   → **所有写操作会静默不落库**，而且现有测试很多自己 `commit`，不一定能发现。
   必须用 `raw_connection()` + 显式 `commit()`，或统一包一层 `db.connect()` 保证与旧语义**完全一致**。

3. **池上限要和应用并发上限一起定，不能各定各的。**
   `deploy/systemd/nutri-graph.service` 是 `--workers 1`，但 FastAPI 的**同步端点跑在 anyio 线程池（默认 40 并发）**。池上限若小于这个并发量，请求会在线程里排队等连接，**比现在"直接新建连接"更慢** —— 这正是上一版 `pool_size=8, max_overflow=12`（合计 20 < 40）的问题。

   → **首选方案是把应用并发收小**（uvicorn `--limit-concurrency`，或 `AnyIO` 线程池上限），让并发上限 ≈ 池上限，而不是把池开到 40。理由：单 worker、LLM 往返占大头，真实并发远低于 40；把池开大只是把等待从"排队等连接"换成"多占 40 条 PG 长连接"，没有收益。
   → 参数：`pool_size=10, max_overflow=5`（**合计 15**）、`pool_timeout=3s, pre_ping=true, recycle=1800`（容器空闲断连是必然发生的）；`limit_concurrency=10`。
   → **15 与 10 的差额 5 是留给后台线程的**，不是余量冗余：`--limit-concurrency` 只限制在途请求，画像抽取（`_defer_profile`）和摘要（步骤 4.5）跑在后台线程里，同样要取连接。并发 10 条请求 ≈ 最多再叠加 3–5 条后台连接，正好落在 15。
   → 二者写在同一处配置里，**改一个必须看另一个**；不要出现"并发 15 + 池 15"这种把连接全部分给请求的配置。
   → **`pool_timeout` 到期必须有明确行为**：现在是悬挂到超时后 500。改为返回 **503 + `code=pool_busy`**，前端显示"系统繁忙，请稍后再试"，并让这个 503 可观测（计数 / 日志）。
   → `api/health.py` 的探活**保持短超时直连**，不进池：池耗尽时健康检查最不该排队。

---

### 1.4 需求 4：多轮对话压缩

**合理，而且是当前体验最痛的一条。**

#### 先回答 4.4：现在有"超阈值自动压缩"的方案吗？

**没有。** 现状是**一段没人调用的半成品**：

- `memory/short_term.py:195 summarize_if_needed()` —— **全仓零调用**（grep 确认）。
- `session.summary` 列存在（`migrations/002_business.sql:70`），只有这条死代码会写它；
  `load_context`（`memory/short_term.py:61`）**根本不读 summary**。
- 所以用户感受到的"多轮"，实际只是 `SHORT_TERM_WINDOW=10` **条消息**被拼进 prompt。

#### 现有实现与 4.1/4.2 有三处语义不符

1. **按"条"不按"轮"。** `load_context` 是 `ORDER BY id DESC LIMIT n`，n 是 message 数不是轮数（函数文档写的是"最近 N 轮"，与 SQL 不一致，改的时候顺手对齐）。
   10 条 = 5 轮（每轮 2 条）。一旦引入 tool 消息（一轮 3–5 条），窗口会缩到 2–3 轮，**且可能在轮中间截断**——正好违反 4.2 的"保持一轮对话的完整性"。
   注意：多轮上下文**是有的**（`generate_node` 通过 `_recent_dialogue` 调 `load_context`），问题不在"没有"，而在"计量单位错了、且没有摘要兜底"。

2. **没有轮次标识。** `message` 表只有 `role / content / created_at`（`002_business.sql:72-80`），没有 `turn_no`。
   靠 `created_at` + 角色配对切轮，遇到下面两种情况就切错：
   - 只落了 user、没落 assistant 的轮次：`guard_node`（`graph/nodes.py:28`）命中红旗后 `_route_after_guard`（`graph/workflow.py:49`）直接 `END`，**memory_write 不执行，这一轮压根不落库**——用户回看历史会"凭空少一句话"。
   - 未来的 tool 消息（一轮多条）。

3. **摘要方向是反的。** `summarize_if_needed` 触发条件是"消息数 ≥ 30"，触发后把**最近 30 条**（含最近几轮）整体摘要写进 `session.summary`——它摘要了本该**完整保留**的最新轮次。

#### 4.2 提到的参考实现（msb_erp_openclaw）不能直接搬

那个项目用的是 deepagents 的 `SummarizationMiddleware`（`middlewares/tools_summarization.py`，框架默认上下文到 85% 触发）+ LangChain 的 **messages 列表**。

而本项目 `generate_node` 根本没有构造 messages 列表——它是把事实拼成**一个字符串**喂给 `invoke_model(prompt)`（`graph/nodes.py:419-437`）。

→ 那套中间件**套不进来**。能借鉴的只有两点：
- 按 message type（human/ai/tool）识别轮边界（`middlewares/memory_update.py` 的做法）；
- 摘要用便宜模型。

**真正的窗口切分必须在记忆层（DB）做**，不能指望框架中间件。

---

### 1.5 需求 5：会话内快速检索问过的问题（头脑风暴）

**这一条写的是"头脑风暴"，说明方案未定。先摊开再选。**

现状：
- `GET /sessions/{id}/messages` 一次返回**整段会话**（`api/history.py:40`），前端 `loadSession` 全量渲染（`frontend/assets/app.js:1156`）。
- `/recent-questions` 是**跨会话**最近 20 条用户原话（`memory/short_term.py:176`）。
- **会话内没有任何检索入口。**

| 方案 | 做法 | 成本 | 解决什么 | 解决不了什么 |
|---|---|---|---|---|
| A 前端本地过滤 | 对已加载的 user 消息做 `includes()` + 高亮 | 0.2d | "我刚问过的"、短会话 | 长会话、记不清原话、换设备 |
| B DB ILIKE | `?q=` 参数（不建索引，见步骤 5.3） | 0.3d | 记不清完整原话的长会话 | 同义不同词（"补肾" vs "肾虚吃啥"）；中文模糊匹配 |
| C pgvector 语义检索 | message 加 embedding | 2d + 每条消息一次 embed | 意思相近的问法 | 成本/延迟/敏感文本二次出网；会话内消息量小，收益不成比例 |
| D 会话内问题目录 | user 消息抽成侧栏 jump list（序号 + 前 20 字） | 0.5d | 快速跳转、一眼看全问过什么 | 不能模糊搜 |
| E 摘要里保留"问过的问题" | 摘要 prompt 强制输出 `已问过：…` | 0（搭需求 4 顺风车） | 超长会话的话题概览 | 精确检索 |

**建议：A + D 先上（同一批前端改动，1d 内），B 作为服务端兜底（`?q=`，0.3d，可延后），C 明确不做，E 作为需求 4 的附带产物。**

理由：本项目是短问答场景，会话平均轮次低，A/D 覆盖绝大部分感受；B 只在"用户记得原话里某个词"时才有增量，成本低可以做；C 要把敏感健康文本再送一次外部 embedding 服务，与合规手册第四节的"数据出境 / 第三方披露"要求冲突，收益不成比例。

**隐私约束**：会话内容属敏感个人信息，检索必须带 `owns()` 归属校验（现有 `list_messages` 已走 `memory.owns`，新接口不能绕过）。

---

### 1.6 需求之外，建议补三件小事

1. **敏感词要覆盖用户可见的**输出**，不只是录入。**（已定为本轮必做，见步骤 2.9）
   `needs_v3` 只提了"录入的词条"，但用户真正看到的是生成回答（`graph/nodes.py` 的 `generate_node`）。政治类词一旦出现在回答里就是事故。
   → 在 `generate` 之后加一道输出扫描。**动作按类别分两种，不是一律整段替换**：political 整段换成固定话术，medical_claim 保留事实、删/改写疗效断言句 —— 详见 §3.7（本节这句是概述，实现以 §3.7 为准）。
   **只扫模型生成的 `answer` 正文，固定免责声明不参与扫描**——`config.py:171 _DEFAULT_DISCLAIMER` 是"不能替代医生诊断与用药指导"，含「诊断」二字，拼进去扫会让每条正常回答都命中。免责声明照旧单独放在卡片上。
   输入侧按 §1.7 处理（只处理 political，不整句拦疗效词）。

2. **`guard_node` 短路轮不落库**是历史缺口（见 1.4 第 2 点），需求 4 顺手修掉，否则"最近 3 轮"会少轮。
   ⚠️ 修的时候**不要顺手给红旗回答打码**：`guard_node` 会复述症状原文（"你提到的「{sym}」属于需要及时就医的情况"），这是有意的设计——用户需要知道是哪一句触发了就医提醒。它和敏感词打码是**两条规则**（一条要复述、一条要遮蔽），合并进同一个判断以后必然互相污染。

3. **需求 3 缺运维验收口径**：改完要看 `pg_stat_activity` 连接数从"随请求线性增长"变成"稳定在上限内"，否则等于没验证。

---

### 1.7 输入侧：只处理 political，其余不在输入侧拦（已定方案）

**先纠正一个会做错的方向：输入侧的整句扫描只针对 `political` 一类。**

疗效类词出现在**问句里**不是"内容违规"，是问法。用户问「菠菜能治疗贫血吗」，这里的「治疗」是提问方式，菠菜是已收录食材 —— 如果整句拦成"我没法当作食材来回答"，就是把一次正常的健康咨询答成了拒答。这类由两处兜住：真要去登记的**词**由 `register()` 拦（步骤 1.5），已经能回答的由**输出扫描**约束（步骤 2.9）。

**所以输入侧只有一条规则：political 命中 → 固定话术 + 打码落库。**

现状链路是 `guard → normalize → link → route → uncatalogued_node`，而"未收录"出口会做两件不该发生的事：

```python
# graph/nodes.py uncatalogued_node
results.append(register(term, who=who, source="chat", near_misses=term_near))
return {"gaps": [f"图谱未收录：{r.food_key or r.term}"], "_food_requests": [...]}
```

- `register()`（`kg/request.py:184`）会把词写进 `kg_ingredient_request`，还会走别名模糊匹配、甚至自动建 `dict_alias_candidate`；
- `gaps` 与 `food_requests` 会把词**原样回显**给前端（"图谱未收录：天安门"）。

> political 命中后根本走不到这里（在 `normalize` 之前就被截住了）。但**待登记的词**仍会走到 `uncatalogued_node` —— 比如用户把「治疗糖尿病」当成一个食材名来问。所以步骤 1.5 在 `register()` 里的拦截**不能因为加了 `sensitive_guard` 就省掉**：前者管的是"问句里出现 political"，后者管的是"要写进库的那个词"。

**所以要堵的是六条路径，不是一条：**

| 泄露路径 | 现状 | 处理 |
|---|---|---|
| 回答正文 | `register().message` 会拼「{term}」 | 固定话术，不含词 |
| `gaps` / `food_requests` | 结构化字段带回前端 | 固定 gap 文案，`food_requests` 留空（**改的是调用方 `uncatalogued_node`，不是 `register()`**，见步骤 1.5b） |
| 登记表 | 写 `kg_ingredient_request` | **`register()` 拒绝写入**（步骤 1.5） |
| 消息落库 | `save_round` 存原话，`session.title` 取首问前 40 字 | 落**打码占位**，见下 |
| **Langfuse trace** | `api/chat.py:75 trace(question)` → `observability/tracing.py:41 input={"question": question}` | 命中时 trace 的 input/output 传占位文本 |
| **画像抽取** | `memory_write_node` 落库后 `_defer_profile(user_id, question, answer)` 把原句再发给模型 | 命中时**跳过**画像抽取 |

**落库这条最容易被忽略**：`memory/short_term.py` 的 `title` 就是首问前 40 字，如果第一句被命中，会话标题直接变成那个词，历史面板里天天挂着。

**后两条是"打码"能不能成立的关键**：打码只改了 `message` 表，而原句在图运行之前就已经进了 trace、在落库之后又进了画像抽取。这两处必须和 `message` 用**同一份占位文本**（`[内容已过滤]`），否则"库里没有、日志里有"等于没堵。

推荐做法是**打码而不是丢弃**：
- `message.content` **只打码用户侧**，存 `"[内容已过滤]"`；**助手侧保留原文**
  —— 走到这里的 answer 只有两种：政治类固定话术（本来就不含原词），或 `guard_node` 的红旗就医回答（复述的是症状词，§1.6 第 2 点要求保留）。把助手侧打成占位会把「请尽快就医」换掉，是安全风险不是合规收益（步骤 1.9 / 4.3b，用例 1.13）
- 这样需求 4 的"轮次完整性"不被破坏（该轮仍然是一次完整的 human+ai）
- 历史回放、`recent-questions`、摘要输入里都不会出现原词

**节点位置**：新增 `sensitive_guard`，挂在 `guard` 之后、`normalize` 之前。

为什么不复用 `guard_node`、也不放在 `route` 之后：
- 放在 `normalize/link` 之前 → 不做实体链接、不打向量兜底、不产生别名候选，成本与副作用都是零；
- 与 `guard_node` 分开 → 红旗症状的语义是"要就医"，敏感词的语义是"不展开"，两者的回答、埋点、审计口径完全不同，塞进同一个函数以后必然互相污染。

**命中边必须接 `memory_write`，不能接 `END`。** 图上已经有现成范式：

```
# graph/workflow.py
wf.add_edge("uncatalogued", "memory_write")   # 回答是确定性的，跳过生成，但仍要落记忆
wf.add_edge("generate", "memory_write")
```

而 `guard` 命中后走的是 `_route_after_guard → END`（`graph/workflow.py:46-54`），那一轮**根本不落库** —— 这正是需求 4 要修的历史缺口（见 1.4 第 2 点）。敏感词这轮如果照抄 END，步骤 1.9 的打码就永远不会执行。

```
START → guard → sensitive_guard ─┬─(hit)→ memory_write（打码后落库）→ END
                                 └─(miss)→ normalize → link → ...
```

**状态标志统一叫 `_redact`**（`_sensitive_hit` 这个命名在步骤 1.8 / 1.9 之间对不上过一次，不再引入第二个名字）：`memory_write` 见到 `state["_redact"]=True` 就写占位文本、跳过画像抽取。另外要覆盖一个边界：此时只有 `answer`、没有图查询结果，`answer_card(state)` 不能因此抛异常（步骤 1.13 的用例要覆盖）。

**文案**（只用于 political 命中；不能确认"这是食材"，也不能说"暂未收录「X」"）：

> 这个词我没法当作食材来回答。换个具体的食材或搭配问题试试？比如「黑豆和什么一起吃好」。

理由：说"暂未收录"会让用户以为库里迟早会收录，接着反复问，反而把敏感词推给 `hit_count`；说"无法识别"又太像系统报错。这句既结束了话题，也给了唯一正确的下一步。

**审计**：要能回答"这套词表拦了多少、拦在哪"，但**不要把原词写进日志**（日志文件本身会流转、会被备份）。只记 `category / action / 字段 / scene / hash(term) / 时间`；`review` 场景额外记 `bundle_id`（见 §3.1）。词表侧可以记明文（词表本来就是敏感词库，不是用户内容）。

---

### 1.8 自建词表 vs 第三方内容安全服务

**先回答"是词表吗"：是，v3 方案就是自建词表**（DB 表 + 内置兜底 + 按字段分级 + 版本化）。理由：可解释、可审计、零调用成本、能精确控制"同一个词在名称里拦、在描述里放"。

缺点是明确的：**政治类词的覆盖面永远追不上监管口径**，谐音/变体/新词难以及时跟上。

**第三方服务有，而且不贵**（2026-10 查到的官方计费口径）：

| 服务 | 计费项 | 单价 |
|---|---|---|
| 阿里云内容安全 · 文本审核-按次-高级 | `text_guard_advanced` | **15 元 / 万次**（≈0.0015 元/次） |
| 阿里云内容安全 · 敏感内容审核-按次-高级 | `text_sddp_advanced` | 15 元 / 万次 |
| 阿里云百炼 · 内容安全护栏（QwenGuard） | 按 token | 基础 0.0004 元/千 token；高级 0.003 元/千 token |

同类还有腾讯云天御、网易易盾，能力接近，不展开。

**建议：分两阶段，v3 不接。**

| 阶段 | 做法 | 理由 |
|---|---|---|
| v3（现在） | 只上自建词表；`CONTENT_MODERATION_PROVIDER=none \| aliyun` **只留常量 + 注释，不写适配接口、不写调用分支**（见本节末段） | 还没备案、没上架，先把链路和灰度机制跑顺；接第三方要动隐私政策和出境评估，不该卡在功能开发里 |
| 上架前 / 被要求时 | 打开开关，把**政治类**兜底交给 API（其他三类仍走自建词表） | 政治类是唯一"自建词表不可靠"的类别；按 15 元/万次算，日均 1 万次提问也才 15 元/天 |

**接第三方必须同时做的事**（别等审核时才想起来）：隐私政策的第三方 SDK 清单加一条"内容安全（阿里云）— 用途：内容合规检测 — 涉及信息：提问文本"。现有清单里 DashScope 已经披露"提问内容"，所以这属于同类披露的增量，成本很小，但漏了就是硬伤。

**调用位置**：只在 `sensitive_guard` 里调，且**只对自建词表未命中、长度 ≥ N 的输入**调（短句"黑豆"没有调用的必要）。调用失败/超时一律视为未命中放行 —— 内容安全服务不能成为 /chat 的可用性依赖。

**v3 的实际落地只有一行常量**：`CONTENT_MODERATION_PROVIDER=none|aliyun`，外加一句注释说明将来的接法。**不写 `if provider == 'aliyun': ...` 的空分支** —— 空适配层既没有验收价值，又会让人误以为已经接通；留常量与注释是为了给上架审核留"已预留"的证据，这两件事成本不一样。

---

## 二、执行计划

### 需求 1：敏感词过滤 + 输入侧 political 出口（3–3.5d）

**① 扫描器与词表**

| 步骤 | 内容 | 落点 |
|---|---|---|
| 1.1 | 迁移 `017_sensitive_word.sql`：`sensitive_word(id, word, category, action, severity, enabled, version, note, created_at, promoted_by, promoted_at)`，唯一键 `(word, category)`；`action ∈ block/review` | `migrations/` |
| 1.2 | 新增 `kg/sensitive.py`：`scan(text) -> list[Hit]`；归一化 = `normalize_text`（**只有 NFKC + 去空白**）+ 去符号 + **简繁折叠自己写**（`kg/normalize.py:56` 的注释写明繁简依赖 opencc，而 opencc 不在 `requirements.txt` 里，函数里也没有这一步，不能假设它已经做了）；按词长排序的多模式匹配；进程内缓存 + `refresh()`；**DB 不可用时回退内置 `_BUILTIN`**（政治 / 疗效断言两类必须有硬编码兜底） | `kg/sensitive.py` |
| 1.3 | 种子数据：把合规手册〇节的词收进迁移，分 `political / medical_claim / marketing / superlative` 四类 | `017_*.sql` |
| 1.4 | 接入 `importer.validate_row`：逐字段扫 **名称 / 别名 / 英文名 / `source` / `note` / `scientific_name`**（payload 键名以 `kg/importer.py:648-650` 为准，**不是 `source_desc`** —— 那是 bundle 表 `kg_food_bundle` 的列）。`scientific_name` 要扫：`kg/merge.py:45 NODE_PROPS_WHITELIST` 里有它，会进主图。`block` → `Message("error", ...)` + `verdict=error`；`review` → `warn` + 写 `payload["_sensitive"]` | `kg/importer.py` |
| 1.5 | 接入 `kg/request.py::register` 开头（批量口以外的最后一道）：命中 `block` → 直接返回 `action="rejected_not_food"`，**不写库、`message` 不含原词** | `kg/request.py` |
| **1.5b** | **改的是调用方**：`uncatalogued_node`（`graph/nodes.py:129-149`）用 `r.term` 拼 `gaps`（`"图谱未收录：{r.food_key or r.term}"`）和 `_food_requests[*].term`，**`register()` 返回什么都会把原词带回响应**。所以当 `r.action == "rejected_not_food"` 时：`gaps` 用固定文案、`_food_requests` 置空、`answer` 用固定话术。只改 `register()` 的返回句堵不住 | `graph/nodes.py` |
| 1.6 | 接入 `kg/fetch.py::_persist` 落库前（USDA 的 `description` 可能带营销词） | `kg/fetch.py:181` |
| 1.7 | `api/admin.py:368` 手工登记、`api/kg.py:37 POST /food/request` 复用同一校验 | `api/admin.py` / `api/kg.py` |

**② 输入侧：political 出口 + 打码落库（§1.7）**

| 步骤 | 内容 | 落点 |
|---|---|---|
| 1.8 | 新增 `sensitive_guard` 节点，挂 `guard` 之后 / `normalize` 之前；**只扫 `political`**；命中即返回固定话术，`gaps` 用固定文案、**`_food_requests=[]`**（`/chat` 读的键带下划线，与 `uncatalogued_node` 一致）、`_redact=True`；**命中边接 `memory_write`，不接 `END`** | `graph/nodes.py` + `graph/workflow.py` |
| 1.9 | `memory_write` 支持打码：`state["_redact"]=True` 时 ①**用户侧** `message.content` 写 `"[内容已过滤]"` ②`title` 同样打码 ③**跳过 `_defer_profile`**（画像抽取会把原句再发一次模型）。<br>⚠️ **助手侧不打码**：走到这里的 answer 只有两种 —— 政治类固定话术（本来就不含原词）、或 `guard_node` 的红旗就医回答（复述的是**症状词**，§1.6 第 2 点明确要求保留）。打码助手侧会把「请尽快就医」换成占位，是安全风险，不是合规收益。生成正文那一侧由输出扫描（步骤 2.9 / §3.7）负责 | `memory/short_term.py` + `graph/nodes.py` |
| 1.9b | **Langfuse 要改两处，不是一处**：①`trace()` 在进入图之前就把原句写进 `input`（`api/chat.py` → `observability/tracing.py`）②`with` 退出时 `finally` 又用同一个 `question` 写 `output={"question": question}`，**会盖掉 `record_result` 刚写上的回答**。所以：`agent.invoke` 返回后若 `result["_redact"]`，把该 trace 的 input 与 output 一起改成占位常量；同时**把 `finally` 里对原始 `question` 的写回删掉或改成占位**（顺带修掉"trace output 被问句覆盖"这个既有问题）。占位常量与 `message`、画像跳过共用同一个 | `api/chat.py` + `observability/tracing.py` |
| 1.10 | 审计表 `sensitive_hit_log(id, scene, category, action, field, term_hash, source, bundle_id, word_version, created_at)`：`scene ∈ input/output/import/review`；**不存原词** | `migrations/017_*.sql` |
| 1.11 | 配置：`SENSITIVE_GUARD_ENABLED`；`CONTENT_MODERATION_PROVIDER=none\|aliyun` **只留常量 + 注释，不写适配分支** | `config.py` |

**③ 验证**

| 步骤 | 内容 | 落点 |
|---|---|---|
| 1.12 | 单测 `tests/test_sensitive.py`：全角 / 简繁 / 插符号；**误杀回归**——"功效"在描述里必须放行、在名称里必须拦。（**拼音不作为本轮完成条件**：3.2 的流水线只做去符号 + 子串匹配，`zhiliao` 变不出「治疗」；要么把变体当独立词条写进词表，要么删这一条，不要留一个过不了的用例） | `tests/` |
| 1.13 | 输入侧用例：political 命中 → 回答/`gaps`/`_food_requests` 全都不含原词；`kg_ingredient_request` 行数不变；`message.content`（**用户侧**）是占位符、**助手侧是固定话术**；`session.title` 不含原词；**trace 的 input 与 output 都不含原词**；**未调用画像抽取**；`answer_card` 在无图结果状态下不抛异常。**红旗 + 政治词同时出现的问句** → 用户侧打码，助手侧仍是「请尽快就医」原文 | `tests/` |
| 1.14 | 回归用例：正常问句（含"调理""补气""治疗贫血吗"等易误伤词）**不被输入侧拦截**；"菠菜能治疗贫血吗" → 回答里有菠菜、**没有疗效断言**、不是政治类拒答文案（三句同时成立，见 §3.7）；把疗效词/政治词**当作未收录食材名**来问 → 响应 JSON 里的 `gaps` / `_food_requests` 都不含原词，且 `kg_ingredient_request` 不新增行 | `tests/` |

**完成条件**：
- 上传含"治疗糖尿病"的表格 → 该行 `verdict=error` 且不能提交（`api/food_import.py:339` 的 commit 只取 `verdict IN ('ok','warn','duplicate_db')`）；含"毛爷爷"的名称 → 不进队列、不入库；正常"清热解暑"的描述 → 不报错。
- 提问政治类词 → 返回固定话术，响应 JSON 里搜不到该词，`kg_ingredient_request` 不新增行，历史面板标题正常，Langfuse trace 里搜不到该词。
- 提问"菠菜能治疗贫血吗" → **照常回答菠菜**（不是拒答），回答里不出现疗效断言。

---

### 需求 2：敏感词评审（2.5d）

| 步骤 | 内容 | 落点 |
|---|---|---|
| 2.1 | 迁移 `018_sensitive_review.sql`：`kg_food_bundle` 加 `sensitive_hits JSONB DEFAULT '[]'`、`sensitive_reviewed_by TEXT`、`sensitive_reviewed_at TIMESTAMPTZ`、`sensitive_word_version INT`（记当时的**整表**版本号）；新增 `sensitive_word_version(id, version, updated_by, updated_at, note)` 存表级单调版本；审计表补 `bundle_id` 与审核结论字段。**v3 不做词表管理端页面**，词表靠迁移种子 + SQL 更新维护 | `migrations/` |
| 2.2 | `import_bundle.stage_bundles` 落库时扫一遍写 `sensitive_hits`（兜住 `review` 级与存量数据） | `kg/import_bundle.py` |
| 2.3 | **`promote.approve_bundle` 前加硬闸**：重扫 bundle items；有 `block` → `PromoteError`；有 `review` → 要求请求带 `confirm_sensitive=true`，并把审核人与时间落库 | `kg/promote.py:236` |
| 2.4 | **三个批准口必须走同一套扫描，收口到 `kg/sensitive.py::assert_approvable(obj)`**：①`promote.approve_bundle`（`kg/promote.py:236`）②`kg/review.py` 的 CLI ③`api/admin.py:137 _set_status` —— 它直接 `UPDATE kg_candidate SET status=...`，而 `kg/merge.py:168` 消费 `status='approved'`，**这是页面上真在用的关系候选批准口，漏掉它等于闸只关了一半** | `kg/promote.py` / `kg/review.py` / `api/admin.py` |
| 2.5 | **最后一道扫两次，扫描对象按"各自读得到什么"分开写，不能混成一句**：<br>① **bundle 批准时**（`approve_bundle` / `stage_bundles`）扫 `source_desc`、`note`，以及导入 payload 里的 `source`（`_items` 读的是 `source_desc`，不读 `source`；来源原文今天在 sources 的 title 里）<br>② **`kg.merge` 入口**扫**边的 `claim_text`** 和**节点白名单里的文本**（`kg/merge.py:45 NODE_PROPS_WHITELIST`：名称 / 别名 / 性味 / 摘要 / `scientific_name`）。`source_desc` / `note` **不在 merge 读到的行上**，放这里扫等于空转 | `kg/promote.py` / `kg/import_bundle.py` / `kg/merge.py` |
| 2.6 | `GET /admin/kg/bundles` 列表返回 `sensitive_hits` 摘要；`GET /kg/bundles/{id}` 返回逐字段命中 | `api/admin.py:187/203` |
| 2.7 | `admin-web`：`Bundles.vue` 加"敏感词"列；`BundleDetail.vue` 命中字段高亮 + 命中词/分类/建议动作；`block` 命中时 approve 按钮禁用并给出原因；`review` 级需勾选"已人工确认"才能提交 | `admin-web/src/views/` |
| 2.8 | 单测：`block` 禁止 approve（**三个口各来一次**）/`review` 必须 confirm / 审计字段落库 | `tests/test_promote.py` |
| 2.9 | **必做**（1.6 条第 1 点）：`generate` 之后加输出扫描，**只扫模型生成的 `answer` 正文**。⚠️ **按类别分两种动作，不能一律"整段换成固定话术"**——见下方 §3.7。**免责声明不参与扫描**（`config.py:171` 含「诊断」，拼进去每条回答都会命中） | `graph/nodes.py` |

**完成条件**：构造一条 `block` 级 bundle → `approve_bundle` 返回 409、CLI 失败、`_set_status` 也失败（`PromoteError` 文案不含"不存在"时 `api/admin.py:235` 已映射为 409）；`review` 级不带 `confirm_sensitive` → 409；带 → 通过且 `sensitive_reviewed_by` / `sensitive_word_version` 有值。

> ⚠️ 2.5 的用例会写 Neo4j，**必须打 `graph` 标记**（`tests/conftest.py:86` 默认跳过）。2026-10-28 那次误删 120 个线上节点就是这类用例造成的，别再对生产图跑。

---

### 需求 3：连接池（1.5–2d）

| 步骤 | 内容 | 落点 |
|---|---|---|
| 3.0 | **先写落库测试，再改代码**：在替换任何调用点之前，先写一条「写一条 → 换一条连接读得到」的用例跑通（当前语义基线），替换完成后同一条用例必须仍通过。顺序反了的话，事务语义反转会被埋在几十处改动里 | `tests/test_db_pool.py` |
| 3.1 | 新增 `db/engine.py`：`get_engine()` 单例（`postgresql+psycopg://`）；`connect()` 上下文管理器 = `raw_connection()` + **显式 commit** + 异常回滚 + 归还；`session_scope()` 给新代码用；`dispose()`。模块注释里写死两条约定：①退出语义 = 提交，与 `with psycopg.connect()` 一致 ②`scripts/*` 保持直连（`migrate` 还要 autocommit），不要"顺手统一" | `db/engine.py`（新目录） |
| 3.2 | config 增 `PG_POOL_SIZE` / `PG_MAX_OVERFLOW` / `PG_POOL_TIMEOUT` / `PG_POOL_RECYCLE` / `PG_POOL_PRE_PING`，**并与 `UVICORN_LIMIT_CONCURRENCY` 写在同一段**（改一个必须看另一个）。⚠️ **光加配置项没用**：`--limit-concurrency` 必须写进 `deploy/systemd/nutri-graph.service` 的 `ExecStart`，否则启动命令不会读它，池上限仍是孤立的数字 | `config.py` + `deploy/systemd/nutri-graph.service` |
| 3.3 | `app.py` lifespan 里建池 + 启动探活日志（池大小 / 并发上限 / 首个连接是否成功）；shutdown 时 `dispose()` | `app.py` |
| 3.4 | 逐个替换取连接处，**SQL 一行不动**。替换范围 = §1.3 清单 **20 个文件去掉 `api/health.py`**（保持短超时直连）= **19 个业务模块**。**`api/admin_import.py` 要算进去**：它有 7 处 `psycopg.connect`，路由已挂在 `app.py:24`，是管理员批量导入入口，漏了它会留一条每请求新建连接的路径。`scripts/`、`tests/` 不在范围内。**池上限要留余量**：`--limit-concurrency` 只限制在途请求数，画像抽取（`_defer_profile`）和摘要（步骤 4.5）是后台线程，同样占连接 —— 见步骤 3.2 的数字，不要把 15 条全部分给 15 个请求 | 见清单 |
| 3.5 | `scripts/*` **保持直连**（CLI 一次性进程，池无意义；`migrate` 还要 autocommit）——写进 `db/engine.py` 的模块注释 | — |
| 3.6 | 单测 `tests/test_db_pool.py`：①**写操作真的落库**（防事务语义反转）②并发 20 请求后 `pg_stat_activity` 连接数有上限 ③杀掉一条连接后下个请求能自愈 ④池打满时接口返回 **503 + `pool_busy`**（不是悬挂后 500） | `tests/` |
| 3.7 | 上线验收：`systemctl restart nutri-graph` 后观察 `pg_stat_activity` 从线性增长变成稳定 | 服务器 |

**完成条件**：3.6 四条用例通过；连续 100 次 `/chat` 后 `select count(*) from pg_stat_activity where datname='nutri_graph'` ≤ `pool_size + max_overflow`。

---

### 需求 4：多轮压缩（2.5–3d）

| 步骤 | 内容 | 落点 |
|---|---|---|
| 4.1 | 迁移 `019_turns.sql`：`message.turn_no INT DEFAULT 0`、`message.kind TEXT DEFAULT ''`（human/tool/ai）、`session.summary_until_msg_id BIGINT DEFAULT 0`、`session.summary_updated_at`；索引 `(session_id, turn_no)` | `migrations/` |
| 4.2 | `save_round` 改为事务内取 `turn_no = COALESCE(MAX(turn_no),0)+1`（对 session 行加锁），该轮所有消息**一次性写入**，保证"一轮要么整轮落库、要么不落" | `memory/short_term.py:74` |
| 4.3 | 修 `guard_node` 短路轮不落库：命中红旗后仍走 `memory_write`（answer 已定，只是不生成） | `graph/workflow.py` |
| **4.3b** | **⚠️ 4.3 会打开一条新的泄露路径，必须同时做**：`sensitive_guard` 挂在 `guard` **之后**，红旗命中后直接去 `memory_write`，`sensitive_guard` 根本不会跑 —— 于是"同时含红旗症状和政治词"的问句会**原样落库**。所以 `memory_write` 在落库前**自己对问句再扫一次 political**（sink 兜底）：命中就置 `_redact`。理由是不依赖节点顺序：将来任何新增的早退节点都不会绕过它。<br>`_redact` 的作用范围按步骤 1.9：**问句 + 会话标题打码、跳过画像、trace 用占位，但助手侧保留 `guard_node` 已经生成的就医回答**（那句话来自症状词表，不含问句里的政治词；政治类固定话术只用于本轮没有红旗回答时）。<br>为什么不在 `guard` 之前扫：红旗回答是安全提示，优先级高于合规文案，政治扫描抢在前面会把"请尽快就医"变成"我没法当作食材来回答" | `graph/nodes.py` |
| 4.4 | 重写 `load_context`，**分三段拼**（见 §3.4 的读取规则）：最近 `MEMORY_RETAIN_TURNS`(3) 轮**原文**；水位线**已盖住**的更早轮次用摘要；水位线**还没盖住**的更早轮次**仍用原文**。`SHORT_TERM_WINDOW` 语义由"条"改为"轮" | `memory/short_term.py` |
| 4.5 | 新增 `compress_if_needed(session_id)`：**触发条件只有"轮数 > 3"**（token 预算不进触发条件，理由见 §3.4）；只摘要水位线之后、最近 3 轮之前的**增量**；摘要 prompt 固定结构（见 3.4）；**写回时必须把已有摘要一并交给模型，输出合并后的全文再 `UPDATE`**（不是用增量覆盖）；失败不推进水位线、下次重试、不阻断回答 | `memory/short_term.py` |
| 4.6 | 调用时机：`memory_write_node` 落库**之后**起后台线程（沿用 `_defer_profile` 的 daemon 模式，`graph/nodes.py:484`）。**绝不能放在 generate 之前同步调**，否则每轮多一次 LLM 往返 | `graph/nodes.py:525` |
| 4.7 | 并发保护：同一 session 两个请求同时压缩 → 对 session 行 `FOR UPDATE`，取不到锁就跳过本轮压缩 | `memory/short_term.py` |
| 4.8 | `GET /sessions` 返回 `summary` 与 `round_count`（**接口先加**）。⚠️ **前端侧栏是否展示摘要，本轮不作为完成条件** —— 摘要里含过敏/忌口/症状，把它放到会话列表上是另一个决定，单独排 | `api/history.py` |
| 4.9 | 单测：①5 轮 → 最近 3 轮原文 + 更早轮摘要/原文不重不漏 ②≤3 轮不触发 ③**摘要尚未写入时更早轮仍在提示词里**（不能因为后台线程没跑完就整段消失）④**第二次压缩后第一次的摘要还在**（合并写回）⑤一轮多条消息不被切断 ⑥摘要失败不影响回答 ⑦重复调用不产生重复摘要 | `tests/test_sessions.py` |

**完成条件**：造 5 轮对话后，`session.summary` 含第 1–2 轮要点、`load_context` 输出里最近 3 轮是原文且轮次完整、第 3 轮（水位线未盖住时）也是原文；把摘要模型指向一个会 500 的假模型，`/chat` 仍正常返回。

> 需求 4.4 的答案是**没有**（见 1.4），这一条不是"优化"，是"从零建"。

---

### 需求 5：会话内检索（0.8–1.2d）

| 步骤 | 内容 | 落点 |
|---|---|---|
| 5.1 | 前端：消息区顶部加过滤框（只匹配 `role=user`），命中高亮 + 上/下跳转 | `frontend/assets/app.js` |
| 5.2 | 前端：侧栏加"本轮问过"清单（D 方案，序号 + 前 20 字，点击跳转） | 同上 |
| 5.3 | 后端（可选）：`GET /sessions/{id}/messages?q=`，`content ILIKE '%'||q||'%' AND role='user'`，**必须先过 `owns()`**（对不上返回 404，与现有 `list_messages` 同语义）。**不建 `pg_trgm` GIN 索引**（见下方说明） | `api/history.py` |
| 5.4 | 摘要里带上"问过的问题"清单 → `GET /sessions` 直接可见（E 方案，随需求 4 落地）。⚠️ 它依赖 4.5 的**合并写回**：摘要若被增量覆盖，这个清单只会剩下最近被压缩的几问 | `memory/short_term.py` |

**明确不做**：
- 对 message 做 embedding 的语义检索（理由见 1.5 的 C 行）。
- **`020_message_search.sql`（pg_trgm GIN 索引）从本轮删除。** 两个原因：①单次会话的消息量用 `ILIKE` 足够了，索引的收益接近于零 ②索引对中文无效——
  要写准的是：`gin_trgm_ops` **本身支持 LIKE / ILIKE**，失效的是**中文多字节**这一侧：`kg/textsim.py:5-11` 实测 `similarity()` 在中文短词上全是 0.000，多字节不产生公共三元组；另外两字食材名（"黑豆"）也低于三元组常用的 3 字粒度。别把结论记成"ILIKE 用不上索引"，那会在英文 / 拼音场景上答错。

---

## 三、关键设计细节

### 3.1 敏感词表结构

```sql
CREATE TABLE IF NOT EXISTS sensitive_word (
    id           BIGSERIAL PRIMARY KEY,
    word         TEXT NOT NULL,
    category     TEXT NOT NULL,          -- political | medical_claim | marketing | superlative
    action       TEXT NOT NULL DEFAULT 'review',  -- block | review（本轮不设 flag：三档里没有它的行为）
    severity     INT  NOT NULL DEFAULT 2,         -- 1 低 2 中 3 高
    enabled      BOOLEAN NOT NULL DEFAULT true,
    version      INT  NOT NULL DEFAULT 1,         -- 单行回滚用；不是整表版本
    promoted_by  TEXT NOT NULL DEFAULT '',        -- 谁把它升成 block 的
    promoted_at  TIMESTAMPTZ,                     -- 什么时候
    note         TEXT NOT NULL DEFAULT '',        -- 升级依据（当时的三列数字）
    created_at   TIMESTAMPTZ NOT NULL DEFAULT now(),
    UNIQUE (word, category)
);

-- 词表级版本（整表一个号）。审计里记的是这个号，用来回溯「当时用了哪套词表」；
-- sensitive_word.version 是单行计数，只服务于单行回滚，两者不是一回事。
CREATE TABLE IF NOT EXISTS sensitive_word_version (
    id          BIGSERIAL PRIMARY KEY,
    version     INT  NOT NULL,
    updated_by  TEXT NOT NULL DEFAULT '',
    updated_at  TIMESTAMPTZ NOT NULL DEFAULT now(),
    note        TEXT NOT NULL DEFAULT ''
);

-- 命中审计：收紧决策的唯一数据来源（§3.6 的报表读它）
-- 刻意不存原词：日志/审计表只留 hash，词表本身才是明文
CREATE TABLE IF NOT EXISTS sensitive_hit_log (
    id          BIGSERIAL PRIMARY KEY,
    scene       TEXT NOT NULL,          -- input | output | import | review
    category    TEXT NOT NULL,
    action      TEXT NOT NULL,          -- 当时采取的 action：block | review
    field       TEXT NOT NULL DEFAULT '',  -- 名称/别名/备注…；input 场景为空
    term_hash   TEXT NOT NULL,          -- sha256(word)[:16]
    source      TEXT NOT NULL DEFAULT '',  -- chat | excel_import | manual_entry | fcd_crawler | usda_fdc
    bundle_id   BIGINT,                 -- review 场景必填：否则算不出「该词被驳回/被通过」
    word_version INT NOT NULL DEFAULT 1,   -- 命中时的整表词表版本
    created_at  TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS idx_shl_word ON sensitive_hit_log (term_hash, created_at);
```

> `bundle_id` 和 `word_version` 这两列不能省。§3.6 的收紧报表要按词统计 `rejected`（被审核员驳回的比例）和 `passed`（带词却被批准的比例）—— 没有 `bundle_id` 就 join 不到审核结论，那两列算不出来，收紧就只能靠感觉。

字段级策略（决定同一个词在不同字段的不同待遇）：

```python
# 名称类字段：窄口径，词表里所有 action 都当 block
NAME_FIELDS = ("name", "aliases", "english_name", "scientific_name")

# 描述类字段：宽口径，medical_claim 只拦"断言"不拦"功效"二字
DESC_FIELDS = ("note", "source", "source_desc")

# 两套字段的归属以"对象"为准，不是一个全局列表：
#   importer.validate_row 的 payload  → source / note / scientific_name（没有 source_desc）
#   kg_food_bundle 的列              → source_desc / note
#   kg.merge 读到的行                → 边 claim_text + 白名单节点属性
# scientific_name 归名称类：它在 merge 白名单里会进主图，漏扫等于留一条进图通道。
```

### 3.2 归一化 + 匹配

```
text → normalize_text（只有全角转半角 + 去空白，**没有简繁**）
     → 简繁折叠（kg/sensitive.py 自己写，opencc 不在依赖里）
     → 去非汉字/字母数字（治·疗 → 治疗）
     → 多模式匹配（词数 < 500 时直接 in 判断足够；超了再上 Aho-Corasick）
     → Hit(field, word, category, action, offset)
```

**内置兜底词表**（DB 不可用时仍生效，只放 `block` 级）：
`政治类若干` + `治疗/治愈/根治/处方/诊断/疗效/药到病除/抗癌/降三高`。

### 3.3 连接池适配层（语义必须与现在一致）

```python
# db/engine.py
@contextmanager
def connect():
    """与 `with psycopg.connect(...)` 语义一致：正常退出提交，异常回滚。
    区别只是连接来自池、退出时归还而不是断开。
    """
    raw = get_engine().raw_connection()      # SQLAlchemy 的 _ConnectionFairy
    conn = raw.driver_connection             # 真正的 psycopg.Connection
    try:
        yield conn
        conn.commit()                        # ← 这一行是全部关键
    except Exception:
        conn.rollback()
        raise
    finally:
        raw.close()                          # 归还池，不是断开
```

**这是整个需求 3 唯一容易出事的地方**：少了 `conn.commit()` 就是全站写操作静默失效。

### 3.4 轮次切分 + 滚动摘要算法

```
轮 = 一轮对话 = 从一条 human 消息开始，到该轮结束
    本轮消息 = [human] + [tool]* + [ai]（当前系统只写 human/ai）

触发压缩（memory_write 落库后，后台线程）：
    rounds_total = max(turn_no)
    if rounds_total <= MEMORY_RETAIN_TURNS:        # 只按轮数触发
        return
    cutoff_turn = rounds_total - MEMORY_RETAIN_TURNS
    old = SELECT ... WHERE session_id=? AND turn_no <= cutoff_turn
                                AND id > summary_until_msg_id      -- 水位线
    if not old: return

摘要 prompt 固定四段（不自由发挥）：
    1) 用户约束：过敏 / 忌口 / 症状 / 人群（孕妇、老人…）
    2) 已确认未收录的食材（避免重复登记）
    3) 已给出的建议要点（≤5 条，每条 ≤20 字）
    4) 用户问过的问题清单（供需求 5 的 E 方案）

写回：prev = SELECT summary FROM session WHERE id=?
      merged = llm(已有摘要 = prev, 增量 = old)      # 合并，不是覆盖
      UPDATE session SET summary=merged,
                         summary_until_msg_id=max(old.id),
                         summary_updated_at=now()
```

**❗ 读取规则（三段，缺一段就会整段丢上下文）：**

```
load_context(session_id):
    cutoff  = max(turn_no) - MEMORY_RETAIN_TURNS
    recent  = turn_no >  cutoff                              → 原文
    covered = turn_no <= cutoff 且 id <= summary_until_msg_id → 用 session.summary
    pending = turn_no <= cutoff 且 id  > summary_until_msg_id → 仍用原文（摘要还没盖到这里）
```

> 第二段和第三段必须分开。摘要是后台线程写的，第 4 轮刚落库时它还没跑完 —— 如果只拼「摘要 + 最近 3 轮」，第 1 轮此刻**既不在摘要里也不在最近 3 轮里**，会凭空从提示词里消失，而且失败不推进水位线，消息还在库里，属于"数据没丢、体验丢了"的静默故障。

**❗ 写回规则：合并，不是覆盖。** 只把增量交给模型再 `UPDATE summary=?`，第二次压缩会把上一次的摘要换掉，长会话里只剩最后一段增量。

**关于 token 预算**：本轮**不进触发条件**。`cutoff = rounds_total - 3` 决定了轮数 ≤3 时没有可摘要的行、>3 时轮数条件已经触发，预算既不会让保留窗口变薄，也没回答"单轮特别长时怎么压"。**4.4 的答案维持"当前没有按阈值压缩"**。若将来要做，被预算挤出保留窗口的轮次必须先出现在摘要里，读取侧仍走上面的三段规则。

**关键约束**：用户约束（过敏 / 忌口）的**权威源永远是 `user_profile`**（`memory/long_term.py`），摘要只负责"话题衔接"，不能成为健康信息的唯一载体。否则摘要一丢，过敏过滤就失灵。

**`kind` 列**：迁移里建出来（human/tool/ai），**v3 不实现 tool 消息的写入** —— 图里现在只有 human/ai，先留出位置，别为一个不存在的场景写逻辑。

### 3.5 会话内检索接口

```
GET /sessions/{session_id}/messages?q=黑豆&role=user&limit=200
  ① memory.owns(user_id, session_id) 不通过 → 404（沿用现有语义）
  ② q 为空 → 现有行为（全量）
  ③ q 非空 → content ILIKE '%'||%s||'%' AND role='user'（单次会话量小，顺序扫描即可）
  ④ 返回命中片段（前后各 10 字）供前端高亮
```

### 3.6 敏感词的灰度与收紧机制（回答"一周之后怎么收紧"）

**先纠正一个容易做错的前提：不是"词表全上 review，一周后再收紧"。** 政治类根本不能灰度——灰度意味着它有一周时间是放行的。正确的分档是**按争议度**，不是按时间：

| 档 | 范围 | 上线时的 action | 要不要灰度 |
|---|---|---|---|
| 红线 | `political`（任何字段）；`medical_claim` 出现在**名称类**字段 | **block** | ❌ 不灰度 |
| 争议 | `medical_claim` 出现在**描述类**字段；`marketing`；`superlative` | **review**（不阻断，只记录 + 审核页展示） | ✅ 灰度一周 |

理由：政治类没有"正常语料带政治词"的合法场景；而"辅助治疗"这种说法在中医文献里确实存在，一刀切会把正经文献挡在门外 —— 这才是需要观察的那部分。

**一周后收紧 = 四步固定动作，不靠感觉：**

> **本轮不写 `sensitive_report` 脚本**（见本节第 4 条）。下面 Step 1–4 是**人工操作说明**，
> 用 SQL 直接查 `sensitive_hit_log` JOIN `kg_food_bundle` 即可；等这套动作跑顺 1–2 周再固化成脚本。

```
Step 1  出报表（临时 SQL，不写脚本）
        按 sensitive_hit_log.bundle_id JOIN kg_food_bundle，
        按词表逐词算 hash 与 sensitive_hit_log JOIN，按 (word, category, field) 聚合三列：
          hits      命中次数
          rejected  其中被审核员驳回的比例   ← "确实该拦"的证据
          passed    带该词却被批准的比例     ← "误杀"的证据
        （log 里没有明文，靠 hash 反查词表；词被删了就查不到，属于可接受的代价）
        rejected / passed 由 sensitive_hit_log.bundle_id JOIN kg_food_bundle 的
        status / reviewed_at 得出 —— 这是 bundle_id 那一列存在的唯一理由

Step 2  阈值判定（人工对照，等固化成脚本时再写死）
        political                      → 免检，直接升 block
        medical_claim(描述)           → hits ≥ 5 且 rejected ≥ 80% 且 passed = 0
        marketing / superlative       → hits ≥ 3 且 rejected ≥ 90% 且 passed = 0
        hits < 3                      → 保持 review
        passed > 0                    → 一律不升，先看那几个"带词被批准"的样本

Step 3  存量回放（升之前必须做；复用步骤 2.5 的扫描函数，不写新脚本）
        用新词表全量扫 kg_food_bundle_item + dict_alias + 已 merged 节点属性
        + 匿名会话合并进来的历史消息，输出"会被新词表阻断的存量条数"。
        某个词若阻断 > 1% 存量 → 判定词表过宽，先拆细或降回 review。

Step 4  应用 + 留痕
        UPDATE sensitive_word SET action='block', version=version+1,
               promoted_by='<人>', promoted_at=now(), note='hits=..,rejected=..,passed=..'
```

**三条最容易被忽略的约束：**

1. **`hits < 3` 不许升。** 3 次命中证明不了任何事，靠它升 block 等于用噪音做决策。
2. **`passed > 0` 一律不升。** 只要有"带这个词的正常数据被审核通过"，就说明词本身有合法用法（典型："补"、"调理"、"虚"）。
3. **回滚必须是一条 UPDATE。** 词表带 `version`，出问题改回 `action='review'` 即可，不需要发版。所以 `action` 不能硬编码进 `.py`（这也是 §1.1 坚持放 DB 的原因）。
4. **自动升档脚本不进本轮。** 前四步先在人工手里跑一到两周，等 `sensitive_hit_log` 里真有审核结论了，再把它固化成脚本。把"脚本"和"词表首版"绑在同一次发布里，会让第一次上线同时承担"词表对不对"和"脚本对不对"两类错误，定位不了。

**反向指标**：某个词升 block 后一周内，如果"同一用户换说法重问同类问题"的比例明显上升，说明拦错了方向——把正常问法逼成了变体，用户没被挡住，只是回答质量下降了。

**节奏**：每周一跑一次 report → 人工过一遍 → 改词表 → dry-run → 提交。把"收紧"变成例行公事，而不是上线一周后的一次性事件。一个月后回看：`block` 档应该只占词表很小一部分（真红线），如果一个月涨了 3 倍，说明是在靠堆词解决问题，方向已经错了。

---

### 3.7 输出扫描：按类别分两种动作

输出场景**不复用导入场景的 `block` / `review` 语义**——导入是"这条数据要不要入库"，输出是"这句话要不要改写"，两者不是同一个决定。

| 生成正文里命中 | 动作 | 理由 |
|---|---|---|
| `political` | **整段替换**为政治类固定话术 + 记审计 | 政治词出现在回答里就是事故，没有"保留部分内容"的选项 |
| `medical_claim` | **保留食材事实与搭配，删掉或改写含疗效断言的句子**（断言句以句号 / 分号切分后按命中句剔除；剔除后正文为空再退化成"只给事实"的短答），+ 记审计 | 整段替换会让"菠菜能治疗贫血吗"答不出菠菜，与完成条件冲突；不管又会留下疗效断言，同样冲突 |
| `marketing` / `superlative` | 只记审计，不改写（本轮） | 生成侧不出现这两类，出现了也是提示词的问题，不是输出扫描该擦的屁股 |

**完成条件因此是三句话同时成立**（步骤 1.14 的用例按这个断言）：回答里有菠菜（或图谱给出的对应事实）、没有疗效断言、也不是政治类拒答文案。

> 条目 2 的"改写"走规则切句，不额外调模型：输出已经在 `generate` 之后，再加一次 LLM 往返会把 P95 拉高一倍。切句剔除后如果命中句跨了整段，说明提示词本身在诱导疗效表述，应该改提示词，而不是靠输出扫描兜。

---

## 四、风险与"不做的事"

| 风险 | 说明 | 应对 |
|---|---|---|
| **敏感词误杀 > 漏放** | 描述类词（"辅助治疗""调理"）一刀切会把正经文献挡住 | **分档灰度**：红线类直接 block，争议类先 review；一周后按 §3.6 的四步收紧 |
| **原词泄露** | 回答 / `gaps` / `food_requests` / `message` / **Langfuse trace（含 `finally` 的二次写回）** / **画像抽取** 六路都会带上原词，`session.title` 还会把首问挂到历史面板 | 六路全堵（§1.7）；落库存打码占位；日志只记 `term_hash` |
| **第 3 批改红旗落库后重开泄露** | 步骤 4.3 让红旗走 `memory_write`，而 `sensitive_guard` 在 `guard` 之后 → 含政治词的红旗问句不再被扫 | 步骤 4.3b：`memory_write` 落库前自己再扫一次 political（sink 兜底） |
| **把正常问法判成"不是食材"** | 疗效词出现在问句里（"菠菜能治疗贫血吗"）若整句拦截，一次正常咨询会被答成拒答 | **输入侧只扫 `political`**；疗效词由 `register()` 的 block + 输出扫描兜住（1.14 有回归用例） |
| **批准口漏堵** | 页面批准关系候选走 `_set_status` 直改 `kg_candidate`，而 `kg/merge` 消费 `approved` | 三个批准口收口到 `assert_approvable()`（步骤 2.4），逐个写用例 |
| **连接池事务语义反转** | 现有写操作可能静默失效，测试未必发现 | 先写"写操作落库"用例（步骤 3.0），再动代码 |
| **池打满后请求悬挂** | `pool_timeout` 到期当前没有定义行为 | 返回 503 + `pool_busy`，可观测（步骤 3.6 ④） |
| **摘要漂移 / 摘要丢关键信息** | 长会话里过敏、忌口被"压没了" | 水位线 + 固定结构 + 画像为权威源（3.4） |
| **需求 4 与 5 改同一块前端** | 两次改历史面板，返工 | 排一起做（第六节） |
| **测试写生产图** | 2.5 的 merge 闸门用例会碰 Neo4j | 打 `graph` 标记，`ALLOW_GRAPH_TESTS=1` 才跑 |
| **需求 3 与其他需求混在一个发布** | 基础设施改动面 14 处，出问题定位难 | **单独发一次**，观察 24h 再继续 |

**明确不做：**
- 不把现有 SQL 改写成 ORM / Core 表达式（35K 行 SQL 风格，改动面与收益完全不成比例）
- 不对 `message` 做 embedding 语义检索（合规 + 成本，见 1.5）
- 不对用户输入做**拒答 / 报错 / 敏感词提示**（political 走固定话术 + 打码落库；疗效类问句照常回答，见 §1.7）
- **v3 不接第三方内容安全 API**，也不写空适配分支（只留 `CONTENT_MODERATION_PROVIDER` 常量 + 注释，见 §1.8）
- **不做敏感词管理端 CRUD**（词表靠迁移种子 + SQL 更新；完成条件里不含"词表页面"）
- **不做词表自动升档脚本**（先人工跑 1–2 周，见 §3.6 第 4 条）
- **不建 `020_message_search.sql`（pg_trgm GIN）**（见步骤 5.3）
- **前端侧栏展示会话摘要不进本轮**（隐私与信息密度的单独决定，见步骤 4.8）

---

## 五、测试与验收总表

| 需求 | 验收方式 | 关键用例 |
|---|---|---|
| 1 | 上传含敏感词的表格 + 两类提问 | "治疗糖尿病"→ error；"毛爷爷"→ 不入队；"清热解暑"→ 通过；提问政治类词 → 固定话术、响应里搜不到该词、不写登记表、历史标题与 Langfuse trace 里都没有、未触发画像抽取；**"菠菜能治疗贫血吗"→ 照常回答菠菜**（不是拒答） |
| 2 | 构造 block / review 级 bundle | API 409、CLI 409、confirm 后通过且审计字段有值 |
| 3 | `pg_stat_activity` 观察 + 4 条单测（步骤 3.6） | 写落库 / 连接数有上限 / 断连自愈 / 池满返回 `pool_busy` |
| 4 | 造 4–5 轮对话 | 只摘要最早轮、最近 3 轮完整、轮内不截断、摘要失败不影响回答 |
| 5 | 前端交互 + `?q=` | 会话内过滤命中、跳转定位、跨用户 404 |

**测试铁律**（沿用项目既有约定）：
- **不在本机跑**：本地无 venv、连不上 PG/Neo4j。改完 `scp` 到 `8.134.253.167:/opt/code/nutri_graph`，用 `/opt/envs/nutri/bin/python -m pytest`，库指 `nutri_graph_test`。
- 写图用例默认跳过。**不要在本文件里固化 "206 passed / 13 skipped" 这类数字**（新增用例后必然失效）；开工前跑一次全量，把当时的汇总写进发布记录。
- 前端改了 `app.js` / `styles.css` 必须 bump `index.html` 里的 `?v=`（`/assets/` 是 7 天强缓存）。

---

## 六、实施顺序

**不要并行铺开**。依赖关系决定顺序：

```
需求 3（基础设施，单独发一次，观察 24h）
   ↓
需求 1（扫描器）→ 需求 2（依赖 1 的扫描器 + 词表）
   ↓
需求 4（轮次 + 摘要）
   ↓
需求 5（与需求 4 的前端历史面板改动合并排期）
```

| 批次 | 内容 | 工时 | 发布策略 |
|---|---|---|---|
| 第 1 批 | 需求 3 连接池 | 1.5–2d | **单独发**，观察 `pg_stat_activity` 24h |
| 第 2 批 | 需求 1 + 2 | 6–7d | 红线类直接 block，争议类先 review；一周后按 §3.6 收紧（人工，不带脚本） |
| 第 3 批 | 需求 4 + 5 | 3.5–4d | 一起发（共用历史面板改动） |
| 合计 | | **11–12d** | |

> 第二轮评审又补了 4 个步骤（1.5b、3.7 的双动作、4.3b、1.9b 的 `finally`），约 +0.7d，已计入上表。

**与上一版（10–11d）的差**，逐项列出来便于裁剪：

| 方向 | 项 | 增减 |
|---|---|---|
| ➖ | 空适配层、拼音旁路、`020` 索引、侧栏展示摘要、自动升档脚本 | −1.8d |
| ➕ | 三个批准口收口、trace/画像两处打码、输出扫描由可选转必做、摘要三段读取 + 合并写回、池并发收敛与 `pool_busy`、词表版本表与 `bundle_id` | +2.2d |

**可裁剪项**（想压回 10d 以内就砍这两个，都是"延后一版不影响正确性"的）：
- 输出扫描（步骤 2.9，0.5d）—— 输出侧的兜底，入口和审核两道闸已经先上了。
- `?q=` 后端检索（步骤 5.3，0.3d）—— A + D 已经覆盖主场景。

---

## 六点五、实施记录

### 第 1 批：连接池（2026-10-29 已上线）

| 项 | 结果 |
|---|---|
| `db/engine.py` | 新建。`raw_connection()` + 显式 `commit()` / `rollback()` + 归还；池按 **DSN 分别缓存**（`config.PG_DSN` 可能在运行时被改，测试靠 monkeypatch 造"库不可达"） |
| 替换范围 | 19 个业务模块、92 处；`api/health.py` 保持直连；`scripts/` `tests/` 未动；SQL 一行未改 |
| 配置 | `PG_POOL_SIZE=10 / MAX_OVERFLOW=5 / TIMEOUT=3 / RECYCLE=1800 / PRE_PING=true`；`UVICORN_LIMIT_CONCURRENCY=10` 已写进 systemd `ExecStart` 的 `--limit-concurrency 10` |
| `pool_busy` | `db.PoolBusy` → `app.py` 异常处理器 → 503 + `code=pool_busy` |
| 测试 | `tests/test_db_pool.py` 6 条；全量 **218 passed / 13 skipped**（两次连跑一致） |
| 上线验收 | 重启后连打 30 次 `/kg/import/stats`，`pg_stat_activity` 稳定在 2（1 池空闲 + 1 查询端），不再随请求增长 |

**代码评审（`docs/review/code_review_v3.md`）后补的：**

| 项 | 处理 |
|---|---|
| **池满时画像被吞成"没有画像"** —— `_load_profile` 的 `except Exception` 会把 `PoolBusy` 吞掉，于是过敏/忌口排除名单变空，接口仍返回 200 | `_load_profile` 遇到 `db.PoolBusy` **原样抛出**（走 503）；库真的不可用才降级成 `{}`。新增用例锁住这两条分支 |
| 新建物理连接没有超时 —— `pool_timeout` 只管"等空闲连接"，管不到开新连接 | `create_engine(connect_args={"connect_timeout": 3})`，新增 `PG_CONNECT_TIMEOUT` |
| 本地 `uvicorn app:app` 不带 `--limit-concurrency` | `app.py` 的 `uvicorn.run` 显式带 `limit_concurrency=config.UVICORN_LIMIT_CONCURRENCY` |
| `_engines` 无锁，并发首次访问会建出两个池 | 加 `threading.Lock` |
| 提交/回滚失败后仍把坏连接还回池 | 失败时 `raw.invalidate()`，不依赖 `pre_ping` |
| `AliasStore` / `UserProfile` 仍收 `dsn` 但连的是 `config.PG_DSN` | 加注释 + DSN 不一致时告警 |
| 断连用例没独占池（FIFO 下取到的未必是被杀那条） / 503 用例只调处理器 | 断连用例改成 1 条连接的独占池并断言 pid 变了；503 用例改成走真实请求 |

### 补：审核页展示敏感词命中（步骤 2.6 / 2.7，2026-10-29 已上线）

| 项 | 结果 |
|---|---|
| 列表 | `promote.list_bundles` 带 `sensitive_hits`，并算出 `sensitive_block` / `sensitive_review` 两个计数；`Bundles.vue` 加"敏感词"列，审核员在列表上就能看出该先看哪条 |
| 明细 | `promote.get_bundle` 同时返回落库时的 `sensitive_hits` 和**按当前词表实时重扫**的 `sensitive_hits_live`（落库后词表可能更新过，只看旧结果会漏）。前端优先展示实时那份 |
| 动作 | 命中 block → 批准按钮禁用 + 提示"后端会拒绝批准"；命中 review → 必须先勾"已逐条看过命中"，提交时带 `confirm_sensitive=true` |
| 部署 | `npm run build` → 产物同步到 `/var/www/nutri_graph/console/`（不是 `admin-web/dist`） |

### 第 3 批：多轮压缩 + 会话内检索（2026-10-29 已上线）

| 项 | 结果 |
|---|---|
| 迁移 | `020_turns.sql`：`message.turn_no` / `kind`、`session.summary_until_msg_id` / `summary_updated_at`，索引 `(session_id, turn_no)`，存量按两两一轮补 `turn_no` |
| 落库 | `save_round` 在事务内 `SELECT ... FOR UPDATE` 取 `turn_no`，一轮两条共享同一个轮号；红旗短路由直接 END 改为走 `memory_write`（不再"凭空少一句"） |
| 读取 | `load_context` 三段：最近 N 轮原文 / 水位线已盖住用摘要 / **水位线未盖住仍用原文**。第三段是最容易漏的一段 |
| 摘要 | `compress_if_needed`：触发只看轮数；**合并旧摘要**再写回；失败不推进水位线；`FOR UPDATE NOWAIT` 并发保护；后台线程调用（`_defer_compress`），不占请求耗时 |
| 打码兜底 | `memory_write` 落库前再扫一次 political（步骤 4.3b）——红旗那一轮不经过 `sensitive_guard` |
| 检索 | 后端 `?q=`（走 ILIKE，必须先过 `owns()`）；前端过滤框（只匹配 user 消息、高亮、上/下跳转）+ 侧栏"本轮问过"目录；`?v=` 已 bump |
| 测试 | `tests/test_turns.py` 11 条；全量 **246 passed / 13 skipped** |

**注意**：`MEMORY_RETAIN_TURNS=3` 是**轮**数；`SHORT_TERM_WINDOW` 语义已由"条"改为"轮"，旧名只保留作上限保护。

### 第 2 批：敏感词（2026-10-29 已上线）

| 项 | 结果 |
|---|---|
| 词表 | 迁移 `018_sensitive_word.sql`（词表 + 整表版本 + 命中审计，22 条种子）+ `019_sensitive_review.sql`（bundle 审计字段）。审计只存 `term_hash`，不存原词 |
| 扫描器 | `kg/sensitive.py`：`scan` / `scan_obj` / `scan_political` / `rewrite_answer` / `assert_approvable`；归一化 = NFKC + 简繁（自写小表）+ 去符号；库读不到时回退内置兜底 |
| 录入/导入 | `importer.validate_row` 按字段分级（名称拦、描述提示）；`import_bundle._stage_one` 落库时写 `sensitive_hits`；`fetch._persist` 落库前扫，命中 block 整条不落 |
| 登记 | `register()` 拒绝写入 + `uncatalogued_node` 不回显（`gaps` / `_food_requests` 都摘掉） |
| 输入侧 | `sensitive_guard` 只扫 political，命中边接 `memory_write`；只打码用户侧 + 标题，跳过画像；`trace()` 的 input/output 与 `finally` 写回都改成占位 |
| 三道闸 | `approve_bundle` / CLI `kg.review` / `api.admin._set_status` 全部过 `assert_approvable()`；`kg.merge` 扫 `claim_text` 兜底 |
| 输出扫描 | `_complete_from_prompt` 后按类别两种动作（政治整段替换、疗效按句剔除保留事实），免责声明不参与 |
| 测试 | `tests/test_sensitive.py` 16 条；全量 **235 passed / 13 skipped** |

**实现中撞到的两个真问题：**

1. **`_redact` 没在 `RAGState` 里声明 → 打码根本没生效。** `graph/state.py` 是 `TypedDict`，LangGraph 会把没声明的键丢掉：节点内 `return {"_redact": True}` 看着成功，下一个节点 `state.get("_redact")` 已经是 False。端到端用例才暴露出来 —— 只测节点是测不出来的。
2. **psycopg3 的 `Connection` 没有 `executemany`**（只有 cursor 有），审计批量插入静默失败（被 try/except 吞掉，只在日志里留一行 warning）。

**还没做（本轮范围外）**：无 —— 审核页展示命中（步骤 2.6 / 2.7）已在同日补齐，见下。

**过程中修掉的两个既有问题：**

1. `tests/test_import_manual.py` 的额度按「IP × 24 小时」计数，会**跨轮累积**——一天跑六七遍全量用例就攒够 40 条，整组变 429。已加 autouse fixture 在每条用例前清掉 TestClient 这个 IP 的计数。
2. psycopg3 的 `Connection.__exit__` 在 commit 之后会 `close()`（只有 psycopg 自己的池才会设 `_pool` 跳过）。所以**不能对池里的连接再套 `with conn:`**，一律 `with db.connect() as conn:`。这条已写进 `db/engine.py` 的模块注释。

---

## 七、评审修订记录（2026-10-29）

来源：`docs/review/review_v3.md`。以下为**已采纳**的修订；未采纳的两条单独列在最后。

| # | 修订 | 原文位置 |
|---|---|---|
| 1 | 输入侧**只处理 `political`**；疗效类出现在问句里不整句拦（那是问法不是词条），由 `register()` 的 block + 输出扫描兜住。摘要表那句"入参走未收录出口"同步改掉 | §1.7、零章表 |
| 2 | 命中边**接 `memory_write` 而非 `END`**（沿用 `uncatalogued → memory_write` 的既有范式），否则步骤 1.9 的打码永不执行；标志名统一为 `_redact` | §1.7、步骤 1.8/1.9 |
| 3 | 补两处原词出口：Langfuse trace（`api/chat.py` → `observability/tracing.py`）与画像抽取（`_defer_profile`），三处占位文本用同一常量；命中时跳过画像抽取 | §1.7、步骤 1.9b |
| 4 | 归一化：明确 `normalize_text` 只有 NFKC + 去空白，**简繁折叠自己写**（opencc 不在依赖里）；拼音从完成条件删除或改词表变体 | 步骤 1.2 / 1.12 |
| 5 | 批准口从 2 条补到 **3 条**：`approve_bundle` / CLI / `api/admin.py:_set_status`，收口到 `assert_approvable()` | 步骤 2.4 |
| 6 | 扫描字段改为 `source_desc` / `note` / `claim_text`，**不是节点 `summary`**（后者是 `f"{name}（{category}）"`） | 步骤 2.5 |
| 7 | 输出扫描定为**必做**，且**只扫生成正文**——`config.py` 的默认免责声明含「诊断」，拼进去每条回答都会命中 | 步骤 2.9、§1.6 |
| 8 | 审计补 `bundle_id` 与**表级** `sensitive_word_version`（词行上的 `version` 只用于单行回滚）；`flag` 这一档没有行为，删掉 | §3.1 |
| 9 | 连接池清单补到 **19 个运行时模块**（原漏 `api/auth.py`、`kg/fetch.py`、`kg/import_bundle.py`、`kg/normalize.py`）；`api/health.py` 保持直连 | §1.3、步骤 3.4 |
| 10 | 池上限改为**收小应用并发**（`limit_concurrency`）而不是把池开大；`pool_timeout` 到期返回 503 + `pool_busy` | §1.3、步骤 3.2 |
| 11 | 连接池**先写"写了真的落库"用例再改代码**（步骤 3.0） | 步骤 3.0 |
| 12 | 摘要读取改为**三段**（原文 / 已覆盖用摘要 / 未覆盖仍用原文），写回改为**合并旧摘要**；token 预算移出触发条件，4.4 维持"当前没有" | §3.4、步骤 4.4/4.5 |
| 13 | 侧栏展示会话摘要**不进本轮**完成条件（摘要里含过敏/症状，是单独决定） | 步骤 4.8 |
| 14 | 删 `020_message_search.sql`；理由改写为"gin_trgm_ops 支持 LIKE/ILIKE，失效的是中文多字节与 <3 字查询" | 步骤 5.3、§1.5 |
| 15 | 删空适配层（只留常量 + 注释）、自动升档脚本、词表管理端 CRUD | §1.8、§3.6、步骤 2.1 |
| 16 | 不再在本文件固化 "206 passed / 13 skipped"，改为"开工前跑一次全量，把当时的汇总写进发布记录" | 第五节 |

**评审中未被完全采纳的两条：**

- *"把池上限提到与 anyio 线程池同一量级"* → 方向反过来：首选收小应用并发（`--limit-concurrency`），把池开到 40 只是备选。单 worker + LLM 往返占大头，真实并发远低于 40，多开连接没有收益。
- *"删掉第三方适配"* → 只删分支代码，保留 `CONTENT_MODERATION_PROVIDER` 常量 + 注释：它是给上架审核看的"已预留"证据，成本为零。

**评审之外另行补的两条：**

- `memory/anonymous_merge.py`：匿名会话合并到实名账号时，**存量里没打码的词**仍会进入历史 —— 归进 §3.6 Step 3 的"存量回放"一起清洗，不单独开条目。
- `sensitive_guard` 命中后走 `memory_write`，此时只有 `answer`、没有图查询结果，`answer_card(state)` 不能抛异常 —— 写进步骤 1.13 的用例。

### 7.2 第二轮评审补的修订（同日复审后）

来源：`docs/review/review_v3.md`（第二轮）。三条是**开工前必须补**的，做完之后对应用例才过得去：

| # | 修订 | 原文位置 |
|---|---|---|
| 17 | **新增步骤 1.5b**：`register()` 拒绝写入不够 —— 调用方 `uncatalogued_node` 用 `r.term` 拼 `gaps` 和 `_food_requests[*].term`，原词照样回显。§1.7 表的「完全不走 `register()`」改为「`register()` 拒绝写入 + 调用方不回显」 | 步骤 1.5b、§1.7 |
| 18 | **输出扫描按类别分两种动作**（新增 §3.7）：political 整段替换；medical_claim **保留事实、删/改写疗效断言句**。「一律整段换成固定话术」与"照常回答菠菜"的完成条件冲突 | 步骤 2.9、§3.7、1.14 |
| 19 | **新增步骤 4.3b**：步骤 4.3 把红旗改成落库后，`sensitive_guard`（挂在 guard 之后）不再运行 → 含政治词的红旗问句原样落库。`memory_write` 落库前**自己再扫一次 political**（sink 兜底，不依赖节点顺序）。不在 guard 之前扫，是因为红旗是安全提示，优先级高于合规文案 | 步骤 4.3b |
| 20 | **步骤 1.9b 补 trace 的第二处**：`trace()` 的 `finally` 用原句写 `output`，会盖掉刚记录下的回答。命中时 input/output 一起改占位，并去掉 `finally` 里的原句写回（顺带修既有问题） | 步骤 1.9b |
| 21 | 扫描字段按对象对齐：payload 的键是 `source` / `note` / `scientific_name`（**不是 `source_desc`**，那是 bundle 的列）；`scientific_name` 在 `kg/merge.py:45` 白名单里会进主图，必须扫；`source_desc` / `note` 不在 merge 结果里，放在 bundle 批准时扫 | 步骤 1.4 / 2.5 |
| 22 | 连接池补三句：`--limit-concurrency` 必须写进 systemd 的 `ExecStart`；后台线程（画像 / 摘要）另占连接，池上限要留余量；替换范围 = 19 − `health` = 18 个业务模块 | 步骤 3.2 / 3.4 |

**同步清掉的过时段落**（分析与步骤互相矛盾的地方，实现一律以步骤为准）：

| 位置 | 已改为 |
|---|---|
| 零章合计工时 | 11–12 人日（与第六节一致；裁剪项仍写在第六节） |
| §1.1 第 3 点 | 归一化的三种情况分开写清：能复用 `normalize_text` 的、要自己写的（简繁）、本轮不做的（拼音） |
| §3.2 流水线 | `normalize_text` 后面去掉「简繁折叠」，简繁单列为一步 |
| §1.8 阶段表 | 「留适配接口」→「只留常量 + 注释」 |
| 第五节需求 3 | 「3 条单测」→ 4 条（含 `pool_busy`） |
| §3.6 Step 1–4 | 命令改为人工 SQL 操作说明，明确本轮不写 `sensitive_report` 脚本 |

### 7.3 第三轮评审补的修订（同日复审后）

来源：`docs/review/review_v3.md`（第三轮）。**方案不用再改**，这四条是"按错句写会漏一个模块 / 盖掉安全提示"的对齐问题：

| # | 修订 | 原文位置 |
|---|---|---|
| 23 | **`api/admin_import.py` 要进替换清单**：它有 **7 处 `psycopg.connect`**，路由挂在 `app.py:24`，是管理员批量导入入口。上一版写"它不自己取连接，不计"是错的（本机 BSD grep 对含中文文件误报为无匹配导致漏查）。替换范围 = 20 − `health` = **19 个业务模块** | §1.3、步骤 3.4 |
| 24 | **池数字与并发数字分开**：`limit_concurrency=10` + 池 `10+5=15`，**差额 5 是留给后台线程**（`_defer_profile` / `compress_if_needed`）的，不是冗余。原来是"并发 15 + 池 15"，与步骤 3.4 的"留余量"直接冲突 | §1.3、步骤 3.2 |
| 25 | **步骤 2.5 按对象拆成两次扫描**：bundle 批准扫 `source_desc` / `note` / payload 的 `source`；`kg.merge` 扫边的 `claim_text` + 白名单节点属性（名称 / 别名 / 性味 / 摘要 / `scientific_name`）。`source_desc` / `note` 不在 merge 读到的行上，放那里扫是空转。§3.1 的 `NAME_FIELDS` 补 `scientific_name` | 步骤 2.5、§3.1 |
| 26 | **`_redact` 不打码助手侧**：红旗 + 政治词同时出现时，步骤 4.3b 置 `_redact` 会把 `guard_node` 生成的「请尽快就医」换成占位 —— 那是安全提示，且复述的是症状词不含政治词。改为：问句 / 标题 / 画像 / trace 打码，**助手侧保留原文**；政治类固定话术只用于本轮没有红旗回答时 | 步骤 1.9、4.3b |
| 27 | 步骤 1.8 的键名改为 **`_food_requests`**（`/chat` 读的是带下划线的键）；§1.6 第 1 点"block 级整段替换"改为指向 §3.7 的两种动作 | 步骤 1.8、§1.6 |
| 28 | §1.7「推荐做法是打码而不是丢弃」里那句"用户侧与助手侧都存固定话术"改为**只打码用户侧**，与步骤 1.9 / 4.3b / 用例 1.13 一致 | §1.7 |

**与 `plan_v4.md` 的先后依赖（不是本轮能定的，先记下来）：**

- v4 的 web 兜底回答是一条**新的生成正文出口**，落地后必须过 v3 步骤 2.9 / §3.7 的输出扫描，否则中文健康网页的疗效承诺会直接进回答。
- v4 说"v3 的敏感词尚未实现，**自带最小词表并留切换位**"。如果 v4 先上，v3 落地时会出现**两套词表**，切换位要写成"读 `sensitive_word` 表，表不存在才回退内置词"，而不是各带一份硬编码。
- 建议顺序：**v3 第 1 批（连接池）独立先发**；v3 第 2 批的 `kg/sensitive.py` 与 v4 谁先上，取决于哪条更急 —— 但无论谁先，`kg/sensitive.py` 必须只有一份。
