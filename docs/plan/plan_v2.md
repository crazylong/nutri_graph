# needs_v2 需求分析与执行计划（v2）

> 来源：`needs_v2.md`（三条需求）
> 评审依据：`docs/review/review_v2.md`（对照仓库实际调用关系，已逐条核对）
> 状态：已按评审修订。搜索词历史当前走方案 B（不建表），跨会话全站统计列为 TODO。
> 说明：本文档只做分析与计划，不含代码实现。

---

## 一、需求合理性分析

### 需求 1：使用 logging_config.py 打印日志

**方向对，但现状与直觉相反——日志系统还没接进进程。**

`utils/logging_config.py` 里的 `configure_logging()`（轮转 + `agent.run.*` 分流 + pid/thread 格式）**全仓库零调用方**。`app.py:29` 启动时用的是 `logging.basicConfig`，格式不含 pid/thread，文件轮转不会发生。

业务模块里 `logging.getLogger("api.auth")` 这类命名已经在用，缺的是根 logger 的 handler。

**结论**：要改的是**启动入口**，不是写文档、也不是把脚本里的 `print` 换成 logger。

---

### 需求 2：用户历史记录 + 重启历史对话

**合理，与现有架构契合。核心是「会话列表 + 归属校验 + 新开会话」，不是建新表。**

关键现状（对照代码）：
- `session` / `message` 表已存在（`002_business.sql`），消息已在 `memory_write_node` 落库。
- ⚠️ `ShortTermMemory.get_session` 的行为是「取 `ended_at IS NULL` 的最新一条，没有才 INSERT」。全仓库没有写 `ended_at` 的语句，所以**每个用户实际只有一条永不结束的会话**。
- ⚠️ `POST /chat` 已经把 body 里的 `session_id` 直接放进图状态，但**没有校验它属于当前用户**。`load_context` / `save_round` 都无归属检查——这是个越权入口：拿别人的 `session_id` 能读他的历史、把消息写进他的会话。

**搜索词历史 = 会话列表 + 消息回放，不需要第二张表。** `message` 里 `role='user'` 的 `content` 就是用户问过的每一句，天然带 `session_id`。详见第三节。

**结论**：需求合理，中等工作量。要做的是「会话生命周期语义 + 归属校验 + 列表/回放接口 + 前端」。

---

### 需求 3：抽取用户特征作长期记忆

**方向对，扩展 `user_profile` 而不是新建，这个选择成立。但抽取/写入链路与标量字段类型不符。**

关键现状（对照代码）：
- `user_profile` 已有 `allergies` / `conditions` / `diet_style` / `avoid_list`，过敏已经在抽（`_defer_profile`）。
- ⚠️ `parse_profile_from_answer` 末尾 `if isinstance(v, list)` 只保留 list。新增的年龄、性别、身高、体重是标量，**抽出来也会被这行丢掉**。
- ⚠️ `UserProfile.update` 对出现的列做 `EXCLUDED` 整列覆盖。模型某轮只提体重却回 `allergies: []` 时，过敏会被写成空——与「空值不覆盖」不是同一条路径。
- ⚠️ 画像只在 `graph_query_node` 加载。走 `vector_fallback` / `uncatalogued` 时 `_user_profile` 是空的。
- ⚠️ `_PROFILE_CUES` 里**已有**「怀孕」「孕妇」，计划上一版说「会漏掉怀孕」是错的。怀孕真正的问题是抽出来没字段放，应进 `symptoms`，不是再加触发词。

**结论**：需求合理。新增年龄/性别/身高/体重/爱好/症状，但要先改解析函数（收标量）和写入语义（空值不覆盖），并把画像加载挪到生成前的公共路径。

---

## 二、执行计划

### 需求 1：日志接进进程（工作量：小）

| 步骤 | 内容 |
|---|---|
| 1.1 | `app.py` 用 `configure_logging()` 替换 `logging.basicConfig`。`configure_logging` 会清根 logger 上的旧 handler，两边不要同时留 |
| 1.2 | 请求路径继续用 `logging.getLogger("模块.子模块")`；`agent.run.*` 分流过滤器已就位，业务代码不需要再选文件 |
| 1.3 | **不做**「替换全部 print」：`scripts/`、`kg/review.py`、`kg/constraints.py` 里的 `print` 是命令行输出，留给操作者看 |
| 1.4 | `docs/logging.md` 后补，不是本需求完成条件 |

**完成条件**：进程起来后，一条 `api.*` 日志出现在 `backend.log`，并按现有轮转配置切文件。

---

### 需求 2：历史记录（会话列表 + 恢复对话，不建搜索词表）

**数据库迁移 `009_history.sql`：**

| 步骤 | 内容 |
|---|---|
| 2.1 | `session` 表加 `title TEXT DEFAULT ''` |
| 2.2 | 建 `session(user_id)` 索引（`message` 已有 `(session_id, created_at)`，不需要重复建） |
| 2.3 | **不建 `search_history` 表**（见第三节方案 B） |

**会话生命周期语义（关键，先定这个再做接口）：**

| 步骤 | 内容 |
|---|---|
| 2.4 | `POST /chat` 带 `session_id`：校验 `session.user_id == 当前 user_id`，对不上返回 404，不落到 `save_round` |
| 2.5 | `POST /chat` 不带 `session_id`：**总是 `INSERT` 新会话**，不再调用现在的 `get_session`（它会复用旧会话） |
| 2.6 | 会话不存在 / 非本人 → 404（`message.session_id` 有外键，未知 id 现在会被 `memory_write_node` 吞成「记忆写失败」） |

**后端 API（新增 `api/history.py` 或并入 `api/chat.py`）：**

| 步骤 | 内容 |
|---|---|
| 2.7 | `GET /sessions` —— 列当前用户会话（id、title、最后消息时间、消息数），带 `user_id` 条件 |
| 2.8 | `GET /sessions/{id}/messages` —— 拉某会话消息（按 `created_at` 正序），只返回当前用户的会话 |
| 2.9 | 列表排序：按该会话最后一条 `message.created_at`，无消息用 `started_at`。不加 `last_active_at`、不靠 `ended_at` |
| 2.10 | 标题用首条用户问题截断，**不打模型**（`session.summary` 已有滚动摘要字段） |

**前端（`frontend/`）：**

| 步骤 | 内容 |
|---|---|
| 2.11 | 侧栏「历史会话」：登录后拉 `/sessions` 渲染，点进去回放 + 继续对话 |
| 2.12 | 「新对话」按钮：清掉本地 `ng.sid`，下一条提问不带 `session_id` |
| 2.13 | 「最近问过」面板（可选）：从 `message` 查最近 user 消息，点一下 = 发一句新问，不是恢复旧会话 |

**加载时机（登录后加载）：**

| 步骤 | 内容 |
|---|---|
| 2.14 | 拉取挂在「已经是登录用户」的入口（登录成功 + `bootstrap()` 已有 token 两条路走同一处），**不能只挂在 `/auth/token` 成功后**——否则刷新后侧栏是空的 |

**列表规模**：先固定上限（约 50 条会话、单会话消息一次取完），不做游标分页。

---

### 需求 3：画像扩展（扩展 `user_profile`，标量 + 数组分列）

**数据库迁移 `010_profile_attrs.sql`：**

| 步骤 | 内容 |
|---|---|
| 3.1 | `user_profile` 新增可空列：`age INT`、`gender TEXT`、`height_cm FLOAT`、`weight_kg FLOAT`、`hobbies TEXT[]`、`symptoms TEXT[]`（失眠/生理期等；`conditions` 继续只放慢性病，生理期**不**并入 conditions） |

**抽取与写入规则（先改链路，再扩提示词）：**

| 步骤 | 内容 |
|---|---|
| 3.2 | 改 `parse_profile_from_answer` 的过滤：list 字段仍只收 list；标量另收 `int`/`float`/`str`，空串和 `null` 不进入更新字典 |
| 3.3 | `UserProfile.update` 写入语义：**本轮更新**——标量非空才覆盖、数组只做并集、空数组不写 |
| 3.4 | `UserProfile.get` 的 `SELECT` 补上新列 |
| 3.5 | 匿名归并 `anonymous_merge.py`：数组并集；标量**哪边有值用哪边，两边都有值时保留目标账号已有值**（避免后登录的匿名画像盖掉已确认的身高体重） |
| 3.6 | 扩写 `PROFILE_EXTRACT_PROMPT`：加 age/gender/height/weight/hobbies/symptoms；**写明单位换算**（斤→千克、只存厘米、gender 收固定少数取值） |
| 3.7 | 触发词收窄：加「岁、身高、体重、公斤、斤、厘米、失眠、生理期、月经、爱好」；**不加「男 / 女 / 喜欢」**（会误触发「男人补锌」「我喜欢吃番茄」，且后句不一定是用户本人）。保留「有触发才调模型」，不改每轮都抽 |
| 3.8 | 画像加载点挪到**生成前的公共路径**，不绑在 `graph_query_node`；保证降级/未收录路径也能拿到画像 |

**画像进入提示词（授权使用）：**

| 步骤 | 内容 |
|---|---|
| 3.9 | `build_grounded_prompt` 里显式授权：年龄/症状这类个人约束**可以用来缩小建议范围，但不能用来编造图谱里没有的关系**（否则会被「禁止常识推断」的禁令抵消）。过敏今天生效是因为进了 Cypher，年龄/症状没有图过滤，提示词是它们唯一的使用点 |
| 3.10 | 爱好按需求存，但不参与图谱过滤、也不必进每轮回答提示词 |

**测试（`tests/`，只测点名且与语义一致的）：**

| 步骤 | 内容 |
|---|---|
| 3.11 | 新会话与续写旧会话、越权 `session_id` 被拒绝 |
| 3.12 | 标量空值不覆盖、数组并集幂等、单位换算（斤→kg） |
| 3.13 | 匿名归并标量保留目标账号非空值 |
| 3.14 | 注销后 `user_profile` 行不存在（删整行，新列自动清除） |
| 3.15 | 不测：分页、`query_norm`、图谱命中标记 |

---

## 三、搜索词历史：方案 B（当前）+ 全站统计 TODO

### 当前实现（方案 B）：不建表，从 `message` 查

`message` 里 `role='user'` 的 `content` 就是用户问过的每一句，已随 `memory_write_node` 落库，天然带 `session_id`。

- 「历史会话 + 重启对话」→ 走 `GET /sessions` + `GET /sessions/{id}/messages`。
- 「最近问过，点一下重发」→ 查当前用户会话下 `role='user'` 的消息，按时间倒序、限制条数。点击是发一句新问，不是恢复旧会话。

**不建 `search_history` 的理由**：`query` 与用户消息重复；`query_norm` 去重会把「以同一句开头」的两段对话合并、破坏会话对应；新表不会自动进匿名归并与 `DELETE /me`，是两处真实漏删。

### TODO（延后）：跨会话全站统计

当出现运营需求——例如「全站用户最常搜什么食材 / 高频问法排行 / 未命中词聚合」——时，再引入独立表 + 归一 + 索引：

```
search_history（待建）
  user_id / query / query_norm / hit / created_at
  + (created_at DESC) 索引，query_norm 做前缀/聚合
```

届时需一并处理：匿名归并、`DELETE /me` 清理、与 `message` 的去重口径。**当前不做。**

---

## 四、风险与建议

1. **画像抽取成本**：保留「有触发才调模型」的预筛，只收窄/扩充触发词，不改每轮都抽（步骤 3.7 已定）。

2. **个人信息合规**：年龄/性别/身高/体重/生理期属个人信息。注销 `DELETE FROM user_profile WHERE user_id` 删整行，新列自动清除——要防的漏删是**未来若建 `search_history` 必须进注销与归并链路**，不是画像新列残留。

3. **会话生命周期语义**：不传 `session_id` 必开新会话、传入则校验归属、列表按最后消息时间排序。不依赖 `ended_at`（现无写入方），也不为排序加冗余字段。

4. **越权防护**：`session_id` 归属校验是本需求的安全前提，必须写进实现，不能留到实现时再想。

---

## 五、总结

三条需求方向都合理，基础设施已就绪，属自然演进。修订后计划与代码现状对齐：

- 需求 1 从「补文档」改为「接日志进进程」（`app.py` 替换 `basicConfig`）。
- 需求 2 从「建 search_history 表」改为「会话列表 + 归属校验 + 新开会话」，搜索词历史走方案 B（从 `message` 查），全站统计列为 TODO。
- 需求 3 从「扩字段」补充为「先改解析（收标量）与写入语义（空值不覆盖），再扩提示词（含单位换算）」，画像加载挪到生成前公共路径。
