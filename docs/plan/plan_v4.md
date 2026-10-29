# needs_v4 需求分析与执行计划（v4）

> 来源：`docs/needs/needs_v4.md`（2026-10-29 08:55 版）
> 分析依据：逐条对照**当前仓库**代码（v3 已于 08:40 前后落地，行号已全部按新文件重算）
> 状态：待评审（第五轮，已并入 `docs/review/review_v4.md` 09:19 版，修订清单见 §九）
> 说明：本文档只做分析与计划，**不含代码实现**。
>
> **用户确认的三条前提**（2026-10-29）
> 1. **当轮就要答案**（不接受"后台补库、下次再问"）
> 2. 触发样例用**需求原句**：「猪脚跟天麻煲汤合适吗」
> 3. web 搜索结果**只作临时回答**，不作为证据进图谱

---

## 零、结论摘要

| 项 | 判定 | 一句话结论 |
|---|---|---|
| 当轮同步搜索 | ✅ 可做 | 搜索放进请求链路，3s / 整段 8s 两道预算，到点回落 |
| 「按现有数据格式保存」 | ✅ 已闭环 | 需求原文已改成「当轮搜索、只作临时回答」，不再和计划冲突 |
| 结果不进图谱 | ✅ | 不写节点、不建边、不写 `kg_food_bundle`、不写候选表；只写临时答案缓存 |
| 政治类 / 疗效词 | ✅ **复用 v3** | `kg/sensitive.py` 已落地，**不自建第二套词表**（见 D2/D3） |
| 参考 `web_search.py` | ✅ | 只借调用方式，字段名等探测脚本实测；生成走项目自己的 `llm.invoke_model` |
| 主要风险 | ⚠️ 网页内容本身 | 中文健康网页是疗效承诺重灾区，药材词（天麻）更甚 → 四层闸 |

**范围**：新增一条「图谱答不了 → 当轮搜网页 → 标注来源回答」的兜底链路。
**不做**：不写图谱、不写 bundle、不改 USDA 抓取链路、**不接管半解析**（§2.3）、**不新建敏感词表**。

合计 **2.75 人日**（含 R6 的 0.25d）。

---

## 一、现状核对：需求原句实际被切成什么

### 1.1 执行路径（需求原句「猪脚跟天麻煲汤合适吗」）

| 步骤 | 代码位置（当前文件） | 行为 |
|---|---|---|
| ⓪ 敏感闸 | `graph/nodes.py:29 sensitive_guard_node`，路由 `:64` | v3 新增。政治类命中 → 固定话术 → `memory_write` → END，**到不了 `web_answer`** |
| ① 归一 | `graph/nodes.py:89 normalize_node` | `normalize_text` |
| ② 实体链接 | `graph/nodes.py:97 link_node` | 字典最长匹配 → 模糊 → 向量三级都不中，两个词进 `_unresolved` |
| ③ 意图 | `kg/query.py:166 detect_intent` | 关键词表（`kg/query.py:104-112`）没有"煲汤"类说法 → 默认 `["pair"]` |
| ④ 计划查询 | `kg/query.py:175 plan_queries` | 只有 `Ingredient` 锚点，`_entities` 为空 → `plans = []` |
| ⑤ 路由 | `graph/nodes.py:239 route_after_link`（判断在 `:249`） | `plans` 空 + `_unresolved` 非空 → `uncatalogued` |
| ⑥ 未收录出口 | `graph/nodes.py:156 uncatalogued_node` | 取前 2 个词（`:168`）逐个 `register()` |
| ⑦ 登记 | `kg/request.py:186 register` | UPSERT `kg_ingredient_request` + 建 `kg_fetch_task`（`:334`） |
| ⑧ 卡片 | `graph/nodes.py:584 answer_card` → `api/chat.py:108` → `frontend/assets/app.js:869` | 渲染「已登记 #408 状态 queued」 |

**结论**：现状不是没兜底，而是兜底只有一句"我不能回答，等审核"——零信息量 + 两个编号。

### 1.2 切词实测

用 `kg/request.py` 的同一套词表（`_SUFFIXES` `:40`、`_BREAK_WORDS` `:51`、`_STRONG_BREAK` `:76`）在本地复算：

| 问句 | `extract_candidates`（`:141`）结果 | 实际登记键（前 2 个，`nodes.py:168`） |
|---|---|---|
| **猪脚跟天麻煲汤合适吗**（需求原句） | `猪脚`、`天麻煲汤` | `猪脚`、**`天麻煲`** ← 剥后缀「汤」（`normalize_food_term` `:116`） |
| 猪脚跟天麻可以一起煲汤吗 | `猪脚`、`天麻`、`煲汤` | `猪脚`、`天麻`（`煲汤` 被前 2 截断丢掉） |
| 我怀孕了，猪脚和天麻能一起煲汤吗 | `我怀孕了`、`猪脚`、`天麻` | **`我怀孕了`**、`猪脚`（天麻被挤出前 2 位） |
| 猪脚天麻当归煲汤合适吗 | `猪脚天麻当归`（6 字一整段） | `猪脚天麻当归`（一个键） |
| 猪脚、天麻、当归煲汤合适吗 | `猪脚`、`天麻`、`当归` | `猪脚`、`天麻` |

结论：

1. 需求原句的登记键是 `天麻煲`，卡片会写「天麻煲 已登记」——**验收必须用需求原句**。
2. 切词只影响登记键与卡片文案，不影响搜索（搜索用原句，答案照样出得来）。
3. 第三行是既有 bug：「我怀孕了」被当成食材登记，还把真食材挤掉 → §七 R6。
4. 第五行说明**逗号/顿号才切得开**，连写 6 个字会并成一个键 → 断言 12 的例句必须带顿号。

**修法（0.25d，含实测副作用）**：`_BREAK_WORDS`（`:51`）补「煲汤」「煲」。

| 方案 | 需求原句 | 「天麻煲和猪脚一起炖」 | 副作用 |
|---|---|---|---|
| 现状 | `天麻煲` ❌ | `天麻煲` ❌ | — |
| +「煲汤」 | `猪脚`/`天麻` ✅ | `天麻煲` ❌ | 无 |
| **+「煲汤」「煲」**（推荐） | ✅ | ✅ | 「煲仔饭」→「仔饭」（新伪词） |
| +「煲汤/炖汤/熬汤/煲」 | ✅ | ✅ | 同上，无额外收益 |

单字「煲」进 `_BREAK_WORDS` 安全：`_STRONG_BREAK`（`:76`）只收 `len >= 2` 加「配/搭」，不影响剥离保险。「仔饭」记为已知噪声，不在 v4 修。

---

## 二、三个必须说清的现状约束

### 2.1 web 数据进不了图谱——但表述要准确

`kg/verify.py:34` 预留了 `"web": 0.3  # 通用网页，只作线索`。**0.3 本身不构成硬否决**（硬否决来自 `verify.py` 的必需字段缺失 `:71`、营养素不足 4 项 `:74`、来源不在白名单 `:79`、别名冲突 `:89`、匹配否决 `:94`）。

真正拦住它的是打分：单源 web、字段齐全、营养素够 4 项时

```
completeness 1.0×0.20 + authority 0.3×0.30 + agreement 0.6×0.30 + conflict 1.0×0.20 = 0.67
```

低于 `FOOD_AUTO_APPROVE_MIN_SCORE=0.9`（`config.py:324`），且 `FOOD_AUTO_APPROVE_MIN_SOURCES=2`（`:325`）也不满足 → **永远 `pending_review`，但审核人仍可人工放行**。

准确说法是：「现有门槛下 web 源不可能自动通过，但可以被人放进图谱」。v4 选择**连 bundle 都不写**，比现有门槛更严，这是刻意的。

### 2.2 `queued` 永远不会变

`kg/fetch.py:334` 的 `_set_request_status` 只在成功路径调用，失败分支直接 return。抓取 3 次失败后任务变 `failed`，`kg_ingredient_request.status` 一直 `queued`——截图那个 `状态 queued` 就是它，且不会再变。

「天麻」USDA 基本不可能收录，所以**对样例这个词，"审核通过后就能查到"是不成立的承诺**（§4.2 与 §5.2 的话术已按此改写）。

### 2.3 半解析：v4 明确不接管

`route_after_link`（`graph/nodes.py:239`）只要 `plan_queries` 非空就走 `graph_query`，未解析词被静默丢掉。例如「牛肉和天麻能一起吃吗」：只回答牛肉的边，天麻消失。

**放弃接管**，理由三条：① 需求样例是两个词都没链接上；② 接管需要先跑一次 `graph_query` 才有事实可传，`web_answer` 分支里 `_graph_results` 从来不会被填充；③ 接管会把整段 `confidence` 压到 0.25，原本能答的那一半反而降级。

**收窄后的路由**：`plans` 为空 **且** `_unresolved` 非空 **且** 开关打开 **且** 意图允许 **且** 无健康自述 → `web_answer`；任一已链接 → 保持今天的 `graph_query`。

---

## 三、设计决策

### D1 —— 当轮同步搜索，只作临时回答，不落库

**决策**：`/chat` 链路内同步调用搜索，产出回答；**不写图谱、不写 `kg_food_bundle`、不写候选表**。

**理由**：① 用户确认要当轮答案；② 网页摘要抽不出可信 per-100g 营养素，让模型抽数值正是 `kg/draft.py` 开头批判的"凭记忆编造"；③ 不落库就没有脏数据回流通道，出问题只需清缓存。

**代价**：同一个词每次都搜（D4 缓存摊平）；图谱覆盖率不因此提升（§七 R2）。

### D2 —— 触发闸：五种情况不搜

| 情况 | 处理 | 理由 |
|---|---|---|
| **(a) 政治类** | **不在 v4 里做判断** | `sensitive_guard_node`（`nodes.py:29`）在 guard 之后、normalize 之前就已短路 → `memory_write` → END，**根本到不了 `web_answer`**。v4 再写一套 = 第二套词表 |
| **(b) 个体化医疗意图**：`contra_population` / `contra_item` / `symptom_remedy` | 不搜，走 `uncatalogued` | "我痛风能不能吃"是个人健康判断，网页不能作依据 |
| **(c) 健康自述**：`mentions_health_constraint(question)`（`graph/boundary.py:150`，词表 `_PROFILE_CUES` `:131-137`） | 不搜 | **光看意图不够**：`INTENT_KEYWORDS`（`kg/query.py:108`）有「孕妇」没有「怀孕/哺乳/宝宝」，「我怀孕了，猪脚和天麻能一起煲汤吗」意图仍是默认 `pair` |
| **(d) 词不像食材**：`is_plausible_food`（`kg/request.py:107`）不通过 | 不搜 | 二次保险 |
| **(e) 意图不在白名单** | 不搜 | 白名单 = `pair, incompatible, nutrition, substitute` |

> 这条线是刻意的：**"怎么搭配"可以给网络参考，"你能不能吃"不行。**

**注意（实现与测试的前提）**：`sensitive_guard_node` 第一句是 `if not config.SENSITIVE_GUARD_ENABLED: return {}`（`config.py:72`，默认 `true`）。断言 4 的成立**依赖这个开关为真**——测试里要显式置位，否则表现是"政治问句去搜了"，最容易诱导实现者回头自建词表。

### D3 —— 四层安全闸（复用 v3，不重建）

**v3 已具备的能力**（`kg/sensitive.py`，词表落在 `sensitive_word` 表，迁移 `018`）：

| 函数 | 行号 | 用途 |
|---|---|---|
| `scan(text)` | `:199` | 通用扫描，返回 `Hit(category, action, ...)` |
| `scan_political(text)` | `:220` | 只筛 `category == "political"` |
| `rewrite_answer(text)` | `:304` | **按句剔除疗效断言**，返回 `(cleaned, hits)`；`nodes.py:515-525` 已在用 |
| `POLITICAL_ANSWER` | `:58` | 政治类固定话术 |
| `MEDICAL_FALLBACK` | `:60` | 疗效断言被剔干净后的兜底话术 |

| 层 | 位置 | 规则 |
|---|---|---|
| L1 输入 | — | 政治类由既有 `sensitive_guard` 处理，v4 不参与；v4 只管 D2 的 (b)(c)(d)(e) |
| L2 结果 | 拿到片段后 | 按下面的**固定顺序**裁剪；一条都留不下才丢该条，三条都空 → 回未收录话术，日志 `blocked='L2'` |
| L3 输出 | 生成后 | **分级**：`political` → 整段丢弃 + 不贴片段；`medical_claim` / 绝对化 → **按句剔除**；**剔空不用 `MEDICAL_FALLBACK`，改贴 L2 留过的片段**；片段也空 → 回未收录话术 |
| L4 展示 | 前端 | 只显示标题 + 站点域名，`target=_blank` + `rel="noopener noreferrer"`，不渲染网页 HTML |

**L2 裁剪顺序（写死，别和"词表 = sensitive 全类别"混在一起）**：

1. 片段里有 `political` → **整条丢**，不回显、不再做句级裁剪。
2. 否则按句丢掉含 `medical_claim`、`marketing`、健康建议型绝对化的句子。
3. **网页裁剪不用 `superlative`**：「最好」会命中「最好先焯水」，做法句被整句删掉。`superlative` 留在表里，继续只交给图谱路径的 `rewrite_answer`。

**`WEB_ANSWER_MIN_CHARS`（40 字）只卡搜索回来的原始片段，不卡裁完的剩余**：家常做法经常就十几个字，卡剩余会把句级裁剪刚留住的内容再打光。**裁完剩一句就留，一句不剩才丢该条**。

> 边界：片段没有句末标点时（「天麻炖猪脚做法简单」），`re.split` 返回整段，命中即整段被删——等同于丢该条，符合预期；别为了"至少留一句"去截半句。

**L2 为什么不能"命中 `medical_claim` 就丢整条"**：

`018_sensitive_word.sql` 的种子里，`治疗`/`诊断`/`疗效` 都是 `medical_claim`，而这三个词在中文健康网页里是高频词。top_k 只取 3 条，全丢就回落成「暂未收录」——用户看到的和今天没区别，功能等于没上线。所以 L2 用**句级裁剪**，只有 `political` 整条丢。

**L3 为什么不能直接调 `rewrite_answer`，也不能用 `MEDICAL_FALLBACK`**：

- `rewrite_answer`（`sensitive.py:304`）只认 `sensitive_word` 里的词，绝对化不在表里；要复用它的**句式**（`re.split(r"(?<=[。！？；!?;\n])")`），不是函数。同构实现放 `webguard.trim_sentences(text, extra_words)`。
- `MEDICAL_FALLBACK`（`sensitive.py:60`）说的是「只能按图谱里收录的食材信息来回答」——样例里的猪脚、天麻**都不在图谱里**，用它会当场自相矛盾。剔空就贴 L2 留过的片段。

**`kg/webguard.py` 只负责 v3 没有的两件事**：

1. **健康建议型绝对化**：不宜 / 禁用 / 忌食 / 必吃 / 一定要 / 绝对不能 / 千万不要 / 长期服用。
   **不写进 `sensitive_word`**：`rewrite_answer` 的句级剔除是**全类别** `scan`，进了表就会把图谱回答里「痛风患者不宜」这种合法注意事项一起剔掉。
   注意与**广告法绝对化**（`superlative`）区分：`最佳 / 最好 / 国家级 / 最有效` 已经在 `sensitive_word` 里（`018` 第 91-94 行），那类**复用表**，但**网页裁剪不调用**（「最好先焯水」是副词用法，会被误删）。
2. **药材词表**（写死，含：天麻、当归、黄芪、党参、人参、西洋参、灵芝、杜仲、山茱萸、铁皮石斛、西红花、三七、何首乌、甘草）+ `is_herbal()` 判定。命中 → prompt 追加硬约束「只谈常见做法与一般注意事项，禁止任何功效、疗效、剂量、疗程、适用人群描述」。
3. **`trim_sentences(text, extra_words)`**：按句切分 + 过滤，词表 = `medical_claim` + `marketing` + 上面那份绝对化常量，**不含 `superlative`、不含 `political`**（political 已在上一级整条丢）。命中审计**直接调 `kg.sensitive.log_hits`（`sensitive.py:340`）**，不要另写一套——它只存 `term_hash`（`:337`）不存原词，正好符合"日志不落原文"。
   调用形如 `log_hits("web", hits, source="chat")`：`sensitive_hit_log.scene` 是自由 `TEXT`、没有 CHECK 约束（`018` 第 40 行只有注释枚举），所以**不需要为它写迁移**，但要把 `:40` 那行注释的枚举补上 `web`。

> **不要按"药食同源目录"分类**：上一版写"目录之外"却举了天麻/黄芪/枸杞子，这几个的归属本来就说不清。写死词表，人工维护。
>
> `is_herbal()` 扫**原问题**。扫切出来的词时，「天麻」会粘在「天麻煲汤」里（补停顿词之前）——即使补了词，扫原问题也更稳。

### D4 —— 缓存与预算：不能拖垮 `/chat`

| 机制 | 方案 |
|---|---|
| 正缓存 | `web_answer_cache`，key = `sha256(PROMPT_VERSION ‖ "\x00" ‖ normalize_text(question))`，TTL 7 天 |
| 缓存键含 prompt 版本 | **必须**：词表/prompt 改了要 bump，否则 7 天内返回旧答案 |
| 负缓存 | **只用于"搜索成功但过滤后 0 条"**。超时、异常、key 为空、超预算、政治类短路**一律不写**，否则智谱抖动一次，同一问题 24 小时都回未收录话术 |
| 全站预算 | `WEB_ANSWER_DAILY_BUDGET=200`/日，**只计真正发出的请求**（缓存命中、被 D2 挡下的都不计） |
| 单用户限流 | 5 次/小时 |
| 硬超时 | `timeout` 包在 SDK 调用外面（参考实现没有 timeout 参数），搜索 3s |
| 整段预算 | 搜索 + 生成合计 8s，到点回落，**不再加长**。`kg/fetch.py` 开头把抓取放离线就是因为它要几十秒；破例进请求链路就必须有硬上限 |
| 搜索成功但生成失败 | **不整段退回未收录**——已经花掉的搜索不能白花：走 L3 的"贴片段 + 来源"，且贴出去的是 **L2 已裁剪过的片段** |
| 搜索前就失败（超时/key 空/超预算） | 回落 `uncatalogued` 话术，且不写负缓存 |
| 降级 | 任何异常都不让 `/chat` 500：**搜索前**失败回 `uncatalogued`，**搜索后**失败贴片段（见上两行）。没有"一律回落"这条兜底 |

### D5 —— 保留登记链路，但话术不能空头承诺

`register()` 照旧调用（`kg_ingredient_request` 是运营看需求的唯一信号），三点约束：

1. **一次请求只登记一次**：`web_answer` 内部先 `register()`，失败回落时不得再调（否则 `hit_count +2`）。做法：把 `uncatalogued_node` 拆出 `_register_unresolved(state)` 与 `_uncatalogued_message(results)`，两节点共用同一份 `results`。
2. **话术只留编号**：`register()` 的 message（`kg/request.py:352`）改成
   `暂未收录「X」。已登记（编号 #N）。`
   - 去掉现在的「审核通过后就能查到」（`:354`）——天麻走 USDA 不会入库。
   - **也不写「审核有结果后会更新知识库」**：审核结果对这类词就是"抓不到"，知识库不会更新，这仍是空头承诺。
   - 同时去掉 `frontend/assets/app.js:876` 的「在这之前我不会给出未核实的内容」——有 web 回答时当场自相矛盾。
3. 搜索失败时同样只说「已登记」，不承诺查得到。开关关闭时这句同样会出现，所以文案必须自身成立。

### D6 —— key：环境变量

需求已改成「apikey 已写入 `ZHIPU_API_KEY` 变量」，实现直接：

```python
WEB_SEARCH_API_KEY = os.getenv("WEB_SEARCH_API_KEY") or os.getenv("ZHIPU_API_KEY", "")
```

建议后续单开 key——共用会互相挤占限流（ERP 跑批时本项目会大面积回落），账单也说不清。

---

## 四、链路改造

### 4.1 图结构（v3 之后）

```
guard → sensitive_guard → normalize → link → route(route_after_link, nodes.py:239)
  ├─ graph_query        任一食材已链接（保持今天行为，含半解析）        ← 不变
  ├─ web_answer         plans 空 且 有未解析词 且 开关开 且 D2 全通过   ← 新增
  │     └─ 失败/超时/拦截 → 内部直接产出 uncatalogued 话术，不二次登记
  ├─ uncatalogued       plans 空 且有未解析词，但不满足 D2              ← 不变
  └─ vector_fallback    其它                                            ← 不变
```

`web_answer` → `memory_write`（跳过 assemble/generate，它自己产出 answer）。
`workflow.py` 现有 `add_node` 在 `:38-48`，条件边在 `:53 / :60 / :66 / :75`。

### 4.2 需求原句改造后的完整走向

> 「猪脚跟天麻煲汤合适吗」

1. `sensitive_guard` 不命中 → 继续
2. 链接失败 → `_unresolved = ["猪脚", "天麻煲汤"]`
3. 意图 `pair` → D2 全通过 → 进 `web_answer`
4. 登记（键 `猪脚` / `天麻`，**前提是 §1.2 补词已做**）→ `#408` / `#409`
5. 缓存未命中 → 预算够 → 搜索 query = **原问题前 60 字**
6. 结果过 L2 → 命中药材词表（天麻）→ prompt 追加禁功效约束 → 生成 → 过 L3
7. 返回：

```
这两个食材都还没进知识库（已登记 #408 / #409）。
以下是网络检索的结果，未经审核，不能作为食疗依据：

常见做法是猪脚焯水后与天麻同炖，天麻多作配料。
关于两者同食，网页上的说法不一：多数做法支持同炖，
也有说法提到猪脚油脂重，同炖时少放油更好入口。
两者是否存在相克，没有找到一致的说法。

参考来源（2 个站点）：
· 天麻炖猪脚的家常做法 — <host>
· 猪脚怎么炖不腻 — <host>

（提示：以上内容来自公开网页，未经人工审核，不能替代医生或营养师意见。）
```

> 这段示例**刻意不含剂量、疗程、功效、适用人群**——它命中了药材词表。
> 黄条只留「已登记 #N」，不写"审核通过后就能查到"。

8. 写缓存（含 `PROMPT_VERSION`）

---

## 五、实施方案

### 5.1 新增文件

| 文件 | 职责 | 要点 |
|---|---|---|
| `kg/websearch.py` | 搜索适配层 | `WebResult(title, content, url, site)`；`search(query, top_k, timeout_s)`；provider 可切（`zhipu_sogou` / `null`）；**普通函数，不是 `@tool`**；参考实现 `join([d.content])` 把 `link` 丢了，URL 必须显式取 |
| `kg/webguard.py` | **只放 v3 没有的**：健康建议型绝对化 + 药材词 + `is_herbal()` + `trim_sentences()` | 政治/疗效/营销**一律调 `kg.sensitive`**，本文件不存这几类词；裁剪词表不含 `superlative` |
| `kg/webanswer.py` | 编排 | 缓存 → 限流 → 预算 → 搜索 → L2 → prompt → 生成 → L3 → 落日志。**不查图、不接 `profile`** |
| `scripts/check_websearch.py` | 探测脚本 | 打印真实响应字段、耗时、条数；**字段名不许猜，先跑它** |
| `tests/test_web_answer.py` | 单测 | 假 client 注入，覆盖 §六 全部断言 |
| `migrations/021_web_answer.sql` | 两张表 | `018/019/020` 已被 v3 占用 |

### 5.2 改动文件（行号均为当前文件）

| 文件 | 位置 | 改动 |
|---|---|---|
| `graph/state.py` | `RAGState`（`:12`） | **新增 `_web_answer: dict[str, Any]`**。v3 的注释已经踩过这个坑：TypedDict 没声明的键会被 LangGraph 丢掉 |
| `kg/request.py` | `_BREAK_WORDS` `:51` | 补「煲汤」「煲」（§1.2） |
| `kg/request.py` | `:352-356` 文案 | 改「暂未收录「X」。已登记（编号 #N）。」，删「审核通过后就能查到」；不引入任何"知识库会更新"的说法 |
| `graph/nodes.py` | `:239 route_after_link` | 增加 `web_answer` 分支（D2 全条件） |
| `graph/nodes.py` | `:156 uncatalogued_node` | 抽出 `_register_unresolved()` / `_uncatalogued_message()`；**顺带剔除含健康自述的候选片段**（R6） |
| `graph/nodes.py` | 新增 `web_answer_node` | 调 `kg.webanswer.answer(state)` 映射进 state |
| `graph/nodes.py` | `:584 answer_card` | 增加 `"web_answer": ...`（`message.meta` 是 jsonb，无需迁移） |
| `graph/workflow.py` | `:38-48` / `:66` | 注册 `web_answer`，条件边加一支，边指向 `memory_write` |
| `api/chat.py` | `:94-108` 返回体 | `/chat` **自己组装**返回体、不调 `answer_card`，两处都要加 `web_answer` |
| `observability/tracing.py` | `record_result` **`:88`** | metadata 增加 `web_used`（v3 之后这个函数下移到 `:88`，旧写的 `:69` 已失效） |
| `graph/boundary.py` | 新增 `build_web_prompt` | 无 `profile` 参数；返回 `{"answer","cited","confidence"}` |
| `frontend/assets/app.js` | `:830 renderAnswer` | `网络参考` chip + 黄条 + 来源 `details`；`:869-876` 登记文案随 `web_answer.used` 切换 |
| `frontend/assets/styles.css` | 末尾 | `.notice.warn` / `.src-list` / `.src-host` |
| `frontend/index.html` | **`:7`（样式）与 `:175`（脚本）** | 现均为 `?v=20261029a`，v4 要换**新的**版本号（如 `20261029b`）——沿用 `a` 会让浏览器 7 天强缓存不更新 |
| `requirements.txt` | LLM 段（`:40` 附近） | `zai-sdk>=0.2.2,<0.3.0`。`from zai import ZhipuAiClient` 属于 **zai-sdk**；ERP 里同时装的 `zai==0.0.2` 是另一个包，**不要装** |

### 5.3 配置项

```python
WEB_SEARCH_PROVIDER       = os.getenv("WEB_SEARCH_PROVIDER", "zhipu_sogou")
WEB_SEARCH_ENABLED        = _bool(os.getenv("WEB_SEARCH_ENABLED", "false"))   # 默认关，灰度
WEB_SEARCH_API_KEY        = os.getenv("WEB_SEARCH_API_KEY") or os.getenv("ZHIPU_API_KEY", "")
WEB_SEARCH_TOP_K          = int(os.getenv("WEB_SEARCH_TOP_K", "3"))
WEB_SEARCH_TIMEOUT_S      = float(os.getenv("WEB_SEARCH_TIMEOUT_S", "3"))
WEB_ANSWER_BUDGET_MS      = int(os.getenv("WEB_ANSWER_BUDGET_MS", "8000"))
WEB_ANSWER_DAILY_BUDGET   = int(os.getenv("WEB_ANSWER_DAILY_BUDGET", "200"))
WEB_ANSWER_USER_HOURLY    = int(os.getenv("WEB_ANSWER_USER_HOURLY", "5"))
WEB_ANSWER_CACHE_TTL_H    = int(os.getenv("WEB_ANSWER_CACHE_TTL_H", "168"))
WEB_ANSWER_MAX_QUESTION   = int(os.getenv("WEB_ANSWER_MAX_QUESTION", "60"))
WEB_ANSWER_INTENTS        = os.getenv("WEB_ANSWER_INTENTS", "pair,incompatible,nutrition,substitute")
WEB_ANSWER_MIN_CHARS      = int(os.getenv("WEB_ANSWER_MIN_CHARS", "40"))
WEB_ANSWER_PROMPT_VERSION = os.getenv("WEB_ANSWER_PROMPT_VERSION", "v1")   # 进缓存键
```

### 5.4 迁移 `021_web_answer.sql`

```sql
CREATE TABLE IF NOT EXISTS web_answer_cache (
    query_hash   TEXT PRIMARY KEY,              -- sha256(PROMPT_VERSION || '\x00' || normalize_text(question))
    question     TEXT        NOT NULL,
    answer       TEXT        NOT NULL DEFAULT '',   -- '' = 负缓存（搜索成功但过滤后 0 条）
    sources      JSONB       NOT NULL DEFAULT '[]',
    provider     TEXT        NOT NULL DEFAULT '',
    hit_count    INTEGER     NOT NULL DEFAULT 0,
    created_at   TIMESTAMPTZ NOT NULL DEFAULT now(),
    expires_at   TIMESTAMPTZ NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_web_cache_expire ON web_answer_cache (expires_at);

CREATE TABLE IF NOT EXISTS web_search_log (
    id           BIGSERIAL PRIMARY KEY,
    user_id      TEXT        NOT NULL DEFAULT '',
    query_hash   TEXT        NOT NULL,
    provider     TEXT        NOT NULL DEFAULT '',
    ok           BOOLEAN     NOT NULL DEFAULT false,
    result_count INTEGER     NOT NULL DEFAULT 0,
    kept_count   INTEGER     NOT NULL DEFAULT 0,
    blocked      TEXT        NOT NULL DEFAULT '',  -- L2|L3|budget|rate|disabled|error
    error        TEXT        NOT NULL DEFAULT '',
    elapsed_ms   INTEGER     NOT NULL DEFAULT 0,
    created_at   TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS idx_web_log_day ON web_search_log (created_at);
```

**隐私**：原问题只进 `web_answer_cache.question`；日志只存 `query_hash`。政治类在 `sensitive_guard` 就结束了，不会写进这两张表。

### 5.5 生成 prompt（新增到 `graph/boundary.py`）

`build_web_prompt(question, snippets, herbal=False)` —— **不接 profile**（不把过敏/慢病带进第三方上下文，也不让模型拿画像编造）。

硬约束：① 只能转述片段，禁止补充常识；② 禁止功效/疗效/治疗/剂量/疗程/人群禁忌的确定结论；③ 片段矛盾时明说"说法不一"；④ 不得声称权威；⑤ `herbal=True` 追加药材专用约束。

返回 `{"answer","cited","confidence"}`。**confidence 上限 0.25**（低于 `TIER_WEAK` 那一档；`WEAK_DISCLAIMER`（`:34`）是一句文案、不是数值，别混），前端据 `web_answer.used` 显示「网络参考」与黄条。

---

## 六、验收标准

远程跑（本地无 venv、连不上 PG/Neo4j）。**DSN 读环境变量，不写口令**：

```bash
# 1. 探测真实响应字段与耗时（不许猜字段名）
/opt/envs/nutri/bin/python -m scripts.check_websearch --query "天麻炖猪脚的做法"

# 2. 单测
set -a; . /opt/envs/nutri/.env.test; set +a
/opt/envs/nutri/bin/python -m pytest tests/test_web_answer.py -q
```

| # | 断言 |
|---|---|
| 1 | **需求原句**「猪脚跟天麻煲汤合适吗」→ `web_answer.used=true`，回答含"未经审核"，含 ≥1 个来源，`graph_hit=false`；**两个 `food_key` 恰好是 `猪脚` 与 `天麻`**（不是 `天麻煲`） |
| 2 | 片段含 `medical_claim` / 绝对化 → **按句裁剪，剩一句就留**（40 字只卡原始片段，不卡剩余）。例：「天麻可治疗头痛。天麻炖猪脚是家常做法。」→ 只留第二句（10 字也要留）；不能因为出现「治疗」就丢掉整条，否则 top_k=3 会被打光 |
| 3 | 片段含 `political` → **整条丢弃**且不回显；全部被丢 → 回落话术，日志 `blocked='L2'` |
| 4 | 政治类问句 → 由 `sensitive_guard` 短路（**测试须先置 `SENSITIVE_GUARD_ENABLED=true`，且 `sensitive_word` 里有一条 political 词**）→ 返回 `POLITICAL_ANSWER`，**没有 `web_search_log` 行、没有 `web_answer_cache` 行** |
| 5 | 「我痛风能吃猪脚吗」→ 意图 `contra_population` → 不搜 |
| 6 | 「我怀孕了，猪脚和天麻能一起煲汤吗」→ `mentions_health_constraint` 命中 → 不搜；**且登记键只有 `猪脚`、`天麻`，不含「我怀孕了」**（R6） |
| 7 | 超时 / SDK 异常 / key 为空 / 超预算 → `uncatalogued` 话术，HTTP 200，**不写负缓存** |
| 8 | 搜索成功但生成失败 / `DASHSCOPE_API_KEY` 为空 → **贴片段 + 来源**，不回退未收录；**贴的是 L2 裁剪过的片段**——输出里不得出现「治疗」「不宜」「长期服用」 |
| 9 | 同一问题连问 3 次 → `web_search_log` 只 1 条，`web_answer_cache.hit_count=2` |
| 10 | 改 `WEB_ANSWER_PROMPT_VERSION` 后同一问题重新搜索（缓存键变） |
| 11 | 一次请求内 `kg_ingredient_request.hit_count` 只 +1（不因回落二次登记 +2） |
| 12 | **「猪脚、天麻、当归煲汤合适吗」**（顿号）→ 候选三个，前两个登记，正文提到当归时不得写"已登记" |
| 13 | `WEB_SEARCH_ENABLED=false` 时：不发起搜索、不写 web 缓存、不出现「网络参考」。**（这不是"零回归"——补停顿词、R6、登记文案在开关关闭时同样生效，另测）** |
| 14 | `build_web_prompt` 的 `confidence` 不超过 0.25 |
| 15 | 生成文本含 `medical_claim` 或绝对化 → **按句剔除**，不是整段丢；**剔空 → 贴 L2 留过的片段，不用 `MEDICAL_FALLBACK`**（那句说"只能按图谱回答"，而样例两个食材都不在图谱里）；含 `political` → 整段丢弃且不贴片段 |
| 16 | 「不宜/禁用」**不在** `sensitive_word` 里（`SELECT count(*) FROM sensitive_word WHERE word IN ('不宜','禁用')` = 0），图谱回答里的「痛风患者不宜」不被剔；同时**网页裁剪不因「最好」丢句**——「最好先焯水」应原样保留（`superlative` 仍在表里，只服务图谱路径的 `rewrite_answer`） |
| 17 | L2/L3 命中审计走 `kg.sensitive.log_hits("web", ...)`，`sensitive_hit_log` 里只有 `term_hash`，**没有原词、没有原问题**；`scene` 无 CHECK 约束，不需要为它加迁移 |

---

## 七、风险、遗留、不做什么

### R0（已闭环）凭证

需求文档里的明文 key 已删除，改为「apikey 已写入 `ZHIPU_API_KEY` 变量」，且该 key 已轮换。约定：需求、计划、评审都不放凭证。

### R1（遗留，可并行）`queued` 永远不变

`kg/fetch.py:334` 只在成功路径推进 status。最小修（0.5d）：失败分支补 `_set_request_status(request_id, 'rejected')`，话术按 status 分支。**不挡 v4**。

### R2（遗留，不在本次范围）图谱覆盖率不会因此提升

- `china_fcd` 权威性 1.0 已预留（`kg/verify.py:33`），`scripts/import_fcd.py` 存在但没数据；
- `insert_chunk`（`rag/client.py:85`）**全仓零调用** → Milvus `kb_chunks` 空集合 → `vector_fallback`（`nodes.py:319`）永远拿不到东西。

### R3 成本与限流混用

共用 `ZHIPU_API_KEY` 时额度、账单、限流互相影响。日预算 200 是保底不是解法。

### R4 合规披露

搜索会把**用户提问原文**发给第三方（智谱）。隐私政策第三方清单新增：「搜索服务（智谱 AI）— 涉及信息：提问文本 — 目的：未收录食材的参考资料检索」。不发 user_id、不发画像、不发历史。**发布前做，不是上线后补。**

### R5 内容风险无法归零

兜底是：所有 web 回答强制"未经审核"前缀 + 来源列表 + 弱置信样式 + 不给剂量/疗程。投诉处置是清 `web_answer_cache` 单条，不必发版。

### R6（0.25d）健康自述被当成食材登记

实测 `我怀孕了，猪脚和天麻能一起煲汤吗` → 登记 `我怀孕了` + `猪脚`，**天麻被挤掉**。

**不要直接用整份 `_PROFILE_CUES` 做子串剔除**：里面有「斤」「岁」「厘米」「公斤」，「二斤猪肉怎么炖」的候选 `二斤猪肉` 会被整段丢掉，一个食材都不登记（实测）。

只用这份子集：`怀孕 哺乳 孕妇 痛风 糖尿 高血压 低血压 肾病 过敏 忌口 忌食 慢病 失眠 生理期 月经`。改完上面那句的登记键正好是 `猪脚`、`天麻`，断言 6 成立；`斤/岁/厘米/公斤` 留在画像抽取里，不参与登记过滤。

### 已知局限（不在本版）

- **半解析静默少答**：「牛肉和天麻能一起吃吗」仍只答牛肉（§2.3）。
- **三词以上**：`_unresolved` 只登记前 2 个（`nodes.py:168`），第三个不登记，但搜索用整句会出现在答案里（断言 12 管这条）。
- 「煲仔饭」切出「仔饭」这类噪声（§1.2）。

### 明确不做

- ❌ 不写图谱 / 不建边 / 不写 `kg_food_bundle` / 不写候选表
- ❌ **不新建敏感词表**：政治/疗效走 `kg/sensitive`，广告法绝对化（`superlative`）复用 `sensitive_word`；`kg/webguard.py` 只放**健康建议型**绝对化与药材词
- ❌ 不做"web 结果当 D 级证据"（用户已定；将来是独立需求，先改 `verify.py` 分档）
- ❌ **不接管半解析**（§2.3）
- ❌ 不做站点白名单（先黑名单 + 词表）
- ❌ 不改 USDA 抓取链路
- ❌ 不做多轮追问的 web 上下文复用（每轮独立）
- ❌ 不改 assemble/generate 的三档逻辑

---

## 八、工作量与发布

| 阶段 | 内容 | 人日 |
|---|---|---|
| M1 后端 | `websearch` / `webguard` / `webanswer` / 节点 / 路由 / 迁移 `021` / 配置 / `_BREAK_WORDS` / R6 | 1.75 |
| M2 前端 | chip / 黄条 / 来源列表 / 文案切换 / 两处 `?v=` bump | 0.5 |
| M3 联调 | 探测脚本、远程单测、灰度观察 | 0.5 |
| — | **合计** | **2.75** |

**发布顺序**：迁移 `021` → 后端（默认关）→ 前端 → 开开关 → 观察 1 天 `web_search_log` 的 `blocked`/`error` 分布 → 再调预算。

**回滚**：`WEB_SEARCH_ENABLED=false` 一个开关，无需回滚代码；缓存表留着不影响任何现有查询。

---

## 九、修订记录

### 第五轮（`review_v4.md` 2026-10-29 09:19，已采纳）

| 评审意见 | 落点 |
|---|---|
| 3.1 40 字门槛和断言 2 的例子互相否定（剩 10 字会被丢） | D3 的 L2 段：`WEB_ANSWER_MIN_CHARS` **只卡原始片段**，"剩一句就留，一句不剩才丢"；断言 2 |
| 3.1 裁剪词表与顺序要说死 | D3 新增「L2 裁剪顺序」三步：political 整条丢 → medical_claim/marketing/绝对化按句丢 → **不用 `superlative`** |
| 3.1 L3 剔空不能用 `MEDICAL_FALLBACK` | D3 的 L3 行 + `MEDICAL_FALLBACK` 说明段（那句说"只能按图谱回答"，而样例食材都不在图谱里）；断言 15 |
| 3.1 断言 16 预期要改 | 断言 16：不宜/禁用不在表里 + **网页裁剪不因「最好」丢句** |

**在评审之外补的两条**：

1. **审计复用 `kg.sensitive.log_hits`**（`sensitive.py:340`），不另写一套；它只存 `term_hash`（`:337`）不存原词，正好满足"日志不落原文"。新增断言 17。
2. **无句末标点的片段**（「天麻炖猪脚做法简单」）：`re.split` 返回整段，命中即整段删——等同于丢该条，符合预期。**不要为了"至少留一句"去截半句**，半句会把主语截掉、生成更难控制。

### 第四轮（`review_v4.md` 2026-10-29 09:09，已采纳）

| 评审意见 | 落点 |
|---|---|
| 3.1 登记话术仍在承诺 → 收成「已登记（编号 #N）。」 | D5(2)、§5.2 `:352-356` |
| 3.2 绝对化表述没落在 L2/L3 上 | D3 的 L2/L3 表整段重写、webguard 段、断言 2 / 8 / 15 / 16 |
| D4 末行「任何异常都只回落」与「生成失败贴片段」矛盾 | D4 降级行改为分"搜索前 / 搜索后" |
| `record_result` 在 `observability/tracing.py:88`，不是 `:69` | §5.2 已改 |

**在评审两条之外补的三条**（照评审原文实现会出问题）：

1. **绝对化不能直接调 `rewrite_answer`**：它只认 `sensitive_word` 里的词，绝对化不在表里。要复用的是**按句切分再过滤**的写法（`sensitive.py:304` 里那句 `re.split`），不是函数本身。
2. **L2 对 `medical_claim` 一律丢整条，会把结果集打光**：`治疗`/`诊断`/`疗效` 都在 `018` 的种子里，是健康网页高频词；top_k=3 全丢就回落成「暂未收录」，用户看到的和今天没区别。改成句级裁剪 + 40 字兜底，只有 `political` 整条丢（断言 2 / 3）。
3. **`superlative` 已经存在**：`最佳 / 最好 / 国家级 / 最有效` 在 `018` 第 91-95 行，属于广告法绝对化 → 复用表；v4 新增的只有"健康建议型绝对化"（不宜/禁用/必吃…），两类不能合并，也不能进表（会把图谱回答里「痛风患者不宜」一起剔掉，断言 16）。

### 第三轮（`review_v4.md` 2026-10-29 08:51，已采纳）

| 评审意见 | 落点 |
|---|---|
| 迁移 `018` 被占 → 用 `021` | §5.4、§8 |
| 政治类改走 `kg/sensitive`，不自建词表 | §零、D2(a)、D3、§5.1、不做清单 |
| 断言 12 例句切不成三个词 → 用顿号句 | §1.2 第 5 行、断言 12 |
| 断言 13 不能叫"零回归" | 断言 13 改写 |
| 登记文案仍在承诺入库（§5.2 与 D5 冲突） | D5(2)、§5.2 `:352-356` 行、§4.2 示例 |
| R6 不能用整份 `_PROFILE_CUES` | §七 R6（列出子集词，`二斤猪肉` 反例） |
| 行号按当前文件重算（v3 落地后整体下移） | §1.1、§1.2、§2.1、§2.2、§5.2 全表 |

**评审本身有三处已过时或不完整，实现按本文为准**：

| 评审写的 | 实际 |
|---|---|
| 「`needs_v4.md` 同文件仍写着『按现有的数据格式保存』」 | 08:55 已改掉，现在是「当轮搜索、只作临时回答」；key 也改成「已写入 `ZHIPU_API_KEY`」。§6 第 1 步作废 |
| 「`index.html` 第 7 行和第 165 行…没动」 | 脚本已在 **`:175`**（页面加了内容），且两处都已是 `?v=20261029a` → v4 要用**新**版本号 |
| 「样例词用 `kg/sensitive.py` 里已有的政治类兜底」 | `_BUILTIN`（`:135`）**只在 DB 读不到时**才用；`sensitive_word` 表为空则 `scan_political` 返回空、什么都不拦。断言 4 必须先确认表里有词 |

### 第二轮（`review_v4.md` 07:29 版，已采纳）

需求原句切词（`天麻煲`）、半解析不接管、健康自述闸、示例去剂量与人群、`graph/state.py`、负缓存范围、缓存键含 prompt 版本、`index.html` 行号、测试命令去口令。

### 本版在评审之外补的两条

1. **断言 4 的隐含前提**：`SENSITIVE_GUARD_ENABLED`（`config.py:72`，默认 true）+ `sensitive_word` 表非空。缺任一条，表现是"政治问句去搜了"，最容易诱导实现者自建第二套词表。
2. **L3 要分级，不是一律整段丢**：v3 的既定策略是 political 整段替换、`medical_claim` 按句剔除（`rewrite_answer`，`nodes.py:515-525` 已在用）。web 回答沿用同一套，避免"同一句疗效断言，图谱回答里被剔除、web 回答里被整段扔"。
