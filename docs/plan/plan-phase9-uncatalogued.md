# Phase 9 方案：未收录食材的发现与补录

> 对应 needs.txt 第 14 条，以及针对它的三轮批评（风险分级 / 0.9 不成立 / LLM 不会联网）
> 本文回答：你提的 1、2 两条改法是否成立；「另外 4 个必补」怎么写；连带缺口怎么办
> 结论日期：2026-10-27　数据源已核实

---

## 0. 先说结论

| 你的提议 | 裁决 | 一句话 |
|---|---|---|
| **1. 库里没有 → 只写进待收录表** | ✅ **对，而且是最正确的简化** | 把第 14 条拆成「登记（同步、廉价）」和「补录（异步、昂贵）」两段，责任分离 |
| **2. 加一个食物营养获取 agent，从待收录表取，从权威来源取** | ⚠️ **方向对，但三处要改** | ①「agent」要降级成 **pipeline**；②「权威来源」要落到**具体 URL/API**，不能让模型自己判断；③ 必须处理**同名异物**（黑豆是第一个坑） |

一句话概括新架构：

```
用户在 /chat 提到「黑豆」
   → 四级实体链接未命中
   → 先判「是不是别名漏收」和「是不是食材」（同步，几个毫秒）
   → 真是新食材：写 kg_ingredient_request（同步，一次 INSERT）
   → 立即回话「暂未收录，已登记」+ 返回可查询的 request_id
   → 后台 pipeline 抓取 → 字段级 grounding 校验 → 落 bundle
   → 人工审核 bundle → 转 kg_candidate → 走唯一写入口 merge 进主图
```

---

## 1. 对提议 1 的评估：✅ 成立，是这个方向上最合理的改法

### 1.1 为什么这是升级而不是「功能缩水」

原第 14 条是「库里没有 → 当场联网抓 → 校验 → 入库」，四个问题全在一个同步请求里：

| 原设计的问题 | 你改成「只登记」之后 |
|---|---|
| 10-40 秒，把 `/chat` 的 P95 打爆 | 一次 INSERT，几毫秒 |
| 用户狂刷同一个词就反复烧 token | `UNIQUE(food_key)` 天然幂等 |
| 抓取失败只能报错，用户无从追踪 | 状态可查：已登记 / 抓取中 / 待审核 / 已收录 |
| 不可重放、不可批量、不可限流 | 后台队列可以重试、批处理、算预算 |

**本质上你砍掉的是「同步」这个错误决策，保留的是「自增长」这个正确目标。** 这是升级。

### 1.2 但必须补 5 点，否则会踩坑

#### ① 先归一，再判「未收录」——**最关键的一条**

「库里没有」有**两种完全不同的情况**，混在一起就是你最在意的「西红柿/番茄建成两个节点」：

| 情况 | 例子 | 正确动作 |
|---|---|---|
| **别名漏收**（东西在库里，叫法没收录） | 乌豆 / 黑大豆 / 黑小豆 → 其实是「黑豆」 | 写 `dict_alias_candidate`（**已有表**），不进待收录 |
| **真·新食材** | 鹰嘴豆、藜麦（假设库里没有） | 写 `kg_ingredient_request` |

判据直接用现有四级漏斗的中间分数（`kg/normalize.py`）：

```
trigram >= 0.85              → 已命中，正常走图查询（现有逻辑）
0.60 <= trigram < 0.85
   或 0.70 <= milvus < 0.82  → 疑似别名 → dict_alias_candidate + 反问用户确认
两者都低于下界               → 真·新词 → kg_ingredient_request
```

**实现上有个小改动**：现在 `EntityLinker.link()` 只返回一个 `unresolved: list[str]`，没带分数（`nodes.py:66` 只存了词）。
需要在 `LinkResult` 里加 `near_misses: list[{mention, candidate_cid, score, method}]`，
`link_node` 才能区分上面两条分支。这是个十几行的改动，不重构。

#### ② 「待收录」不是「审核」，是两张表

- `kg_ingredient_request`：**用户意图**维度（谁、什么词、几次、状态）—— 用户看这个
- `kg_food_bundle`：**抓取产物**维度（一次抓取的快照）—— 审核人看这个

一个 request 可能对应多次抓取尝试（换源、重试），合并计数在 request 上。混成一张表会导致「同一个人刷新 5 次就出现 5 条待审」。

#### ③ 入口校验：先确认「这东西是食材」

不挡的话，用户输入「阿司匹林」「痛风」「给我写首诗」都会进待收录，把队列和图谱一起污染。

零成本的第一道闸：**查现有 `dict_alias` 的 `entity_type`**
```sql
SELECT entity_type FROM dict_alias WHERE alias_norm = %s
-- 命中 Symptom/Population/Taboo → 直接回「『痛风』是人群/症状，不是食材，你想问的是？」
```
再加规则：长度 1-6、不含标点/数字/URL，最后才用 LLM 兜底分类。

#### ④ 复合词要切出中心词

用户会说「黑豆粥」「炒黑豆」「黑豆薏米水」。直接拿整串去查一定是「未收录」，然后抓出一个不存在的食物。

先规则后模型：剥掉常见后缀（粥/汤/茶/汁/粉/糊/干/炒/煮/蒸/炖/凉拌）→ 剩余部分去查；
规则切不出来再用 LLM 抽中心词。

#### ⑤ 提交要限流，状态要可查

- 每人每天最多提交 N 个新词（默认 10），否则这就是个「让 LLM 烧钱」的入口
- `GET /kg/food/request/{id}` → `queued | fetching | pending_review | approved | rejected`
- 「已反馈给管理员」如果用户查不到进度，就是一句空话

---

## 2. 对提议 2 的评估：⚠️ 方向对，三处必须改

### 2.1 「agent」应该降级成 pipeline

你说的「agent」我建议改成**固定流程的 pipeline**（LangGraph 可以，但节点写死、不让模型选工具）。理由：

| agent | pipeline |
|---|---|
| 每步自主决策，**成本不可预估** | 步数固定，成本可算 |
| 同一输入可能走不同路径，**结果不可复现** | 可复现，能回归测试 |
| 出错时不知道是判断错了还是工具选错了 | 每个节点单独可测 |

这里唯一需要模型「判断」的是**打分和字段核对**，那是流程内的一个节点，不是让模型决定下一步干嘛。

### 2.2 数据源已核实，直接落到具体地址

「权威来源」不能写成「让模型上网找权威来源」——模型判断权威性极易被标题党骗。必须**白名单写进配置**。

**① 中国食物成分表 · 官方查询平台（首选，中文食材）**

- 中国疾控中心营养与健康所：`https://nlc.chinanutri.cn/fq/`；国家食物成分数据公众查询平台 `https://fq.chinafcd.org`
- 覆盖 1300+ 条常见食物，**免费查阅**
- **有 `别名或俗名` 和 `英文名称` 两个字段** —— 这两个字段价值极高（见 §2.3）
- 字段：食部、水分、能量(kJ)、蛋白质、脂肪、碳水、膳食纤维、维 A/C/E、钙磷钾钠镁铁锌硒铜锰碘、SFA/MUFA/PUFA
- ⚠️ **没有公开 API**，只能抓 HTML。合规要求：先读 robots.txt、低频（同一条目缓存 ≥30 天）、不并发、标注来源、**不用于商业用途**（数据是国家基础战略资源，商用需授权）

**② USDA FoodData Central（首选，结构化 API）**

- `https://api.nal.usda.gov/fdc/v1`，免费 key 从 `api.data.gov/signup` 领，**公有领域 CC0**
- 限额：注册 key **1000 次/小时/IP**；`DEMO_KEY` 只有 30/小时、50/天（别上线用）
- 端点：`GET /foods/search?query=&dataType=Foundation,SR Legacy`、`GET /food/{fdcId}`、`POST /foods`（批量 20 个，省配额）
- **Foundation / SR Legacy 的数值就是每 100g**，正好是我们 `CONTAINS_NUTRIENT.amount` 的口径
- 常用 nutrient number：`1008` 能量(kcal)、`1003` 蛋白质、`1005` 碳水、`1004` 脂肪、`1079` 纤维、`1093` 钠
- ⚠️ 这是**美国**食物库，中式食材覆盖不全，且命名体系不同

**③ 性味归经（寒热温凉）**

USDA 和成分表都没有，只能来自《既是食品又是中药材的物质目录》/ 典籍。
→ 天然是 **traditional / C 级**，**永远不允许自动通过**。

**组合策略（推荐）**

```
中文名 → 中国官方平台查条目
           ├─ 命中 → 拿到：官方别名 + 英文名 + 成分表数值(kJ/每100g)
           │           ↓ 用「官方给的英文名」去查 USDA
           │        两源交叉 → 一致的字段给高置信，不一致的进人工
           └─ 未命中 → 只用 USDA（英文名由 LLM 给出，置信度降级）
```

### 2.3 关键设计：用官方英文名当桥梁，而不是让 LLM 翻译

如果让 LLM 把「黑豆」翻成英文去查 USDA，翻译一错整条数据就错了。
而中国官方平台**本身就提供英文名称字段**——用它当桥梁，等于把容易错的环节换成官方数据。

### 2.4 黑豆陷阱：同名异物，这是最先会踩的坑

**「黑豆」在中文里通常指黑大豆（*Glycine max*，黑色种皮的大豆）；
USDA 里的 "Black beans" 是黑菜豆（*Phaseolus vulgaris*）——两种完全不同的东西，营养差异明显。**

如果 pipeline 用「黑豆 → black beans」去查，会把菜豆的营养素挂到黑大豆节点上。
这是**看起来很合理、结果完全错**的典型错误，靠 0.9 阈值根本挡不住（数据本身查得到、来源也权威）。

必须做的三道核对：

1. **学名核对**：bundle 里存 `scientific_name`，与图谱已有同科节点比对
2. **分类核对**：大豆属 vs 菜豆属，category 不一致直接硬否决
3. **别名核对**：中国官方平台的「别名」要一并收进 `dict_alias`，避免下次又当成新词

→ 所以 `kg_food_bundle` 里必须有一列 `scientific_name`，且**它属于硬否决项**。

### 2.5 字段级证据定级（一张 bundle 不能只有一个分）

| 字段 | 来源 | evidence_type | level | 能否自动过 |
|---|---|---|---|---|
| 每 100g 营养素含量 | 中国成分表 / USDA Foundation | `measured` | B | ✅ 可自动 |
| 分类、别名、英文名 | 官方目录 / 成分表 | `guideline` | A | ✅ 可自动 |
| 性味归经 | 药食同源目录 / 典籍 | `traditional` | C | ❌ 必人工 |
| `CONTAINS_NUTRIENT` 边 | 同上营养素 | `measured` | B | ✅ 可自动 |
| **任何关系**（推荐搭配/不宜同食/人群禁忌/药物） | 网页 | — | — | ❌ **无论多少分必人工** |

这条表就是上一轮说的「按风险分级而不是按分数」的落地形式。

### 2.6 把「打分」改成「grounding 核对」——这才是解决 0.9 的办法

原设计是「B 模型给一个 0.9 分」。问题是 LLM 自评不是概率、未校准，A/B 同家族还会互相确认幻觉。

改成**逐字段的可验证核对**，大部分环节根本不需要 LLM：

```
对 bundle 里每个字段：
  1. 抓取正文原文存进 bundle（去掉 HTML）
  2. A 抽字段时必须同时给出 source_span（原文片段，≤200 字）
  3. 【确定性检查，不用模型】span 是否真的出现在正文里？
        用规范化后的子串匹配 + 相似度兜底；不在 → 该字段直接置为"无出处"，丢弃
  4. 【模型检查】B 拿 (字段, span) 判断"这段原文是否支持这个值"
        B 只看得到 span，看不到 A 的推理过程
  5. 汇总：有出处且被支持 → 通过；有出处但被否 → 进人工；无出处 → 丢弃
```

两道关键差异：

- **第 3 步是字符串核对，不是模型判断** —— 这一步就能砍掉大部分编造，且成本为零、结果确定
- **第 4 步是语义蕴含判断，不是「你觉得可信吗」** —— 前者可复现，后者是主观打分

然后 0.9 才有意义：它不再是「模型觉得可信度」，而是
**「通过字段数 / 应有字段数 × 来源权重」算出来的可解释比值**。四维加权：

```
完整性 0.20   必需字段齐全率
权威性 0.30   白名单分级：官方库/指南 1.0，权威媒体 0.7，普通网页 0.3
一致性 0.30   ≥2 个独立来源关键字段一致；单源封顶 0.75
无冲突 0.20   与现有图谱矛盾则直接 0 分并否决
```

**硬否决（不看分数，直接进人工）**：含医疗断言 / 与现图矛盾 / 单来源 / 学名或分类冲突 / 疑似注入文本。

---

## 3. 「另外 4 个必补」的具体方案

### 3.1 归一前置

见 §1.2①。改动点：

| 文件 | 改动 |
|---|---|
| `kg/normalize.py` | `LinkResult` 增加 `near_misses`（带分数与候选 cid） |
| `graph/nodes.py::link_node` | 把 `near_misses` 也写进 state |
| `graph/workflow.py` | `route_after_link` 增加「未收录」分支 |
| 新 `kg/request.py` | 登记逻辑：别名回流 / 待收录 两条出口 |

**注意**：不做这一步，第 2 条那条 pipeline 会不断给「乌豆」造新节点，图谱越跑越脏。

### 3.2 bundle 作为审核单元

**状态机**

```
kg_ingredient_request:  queued → fetching → fetched → pending_review → approved / rejected
kg_food_bundle:         draft  → verified → pending_review → approved / rejected
```

**审核通过后不直接写 Neo4j**，而是转成 `kg_candidate` 行，复用已有的 `kg.merge`：

```
bundle.approved
   → 为每个通过的 item 生成一行 kg_candidate(batch_id = bundle-<id>)
   → python -m kg.merge --batch-id bundle-<id>
```

**为什么这样绕一下**：主图必须只有一条写入口。绕一下的代价是几条 INSERT，收益是
审计口径统一、可整批回滚（`merge.rollback`）、`review_status` 字段语义一致。
如果 bundle 自己写图，以后查「这条边是谁加进来的」就分叉了。

**表结构**（在 `002_business.sql` 里，与 §4.1 的迁移收编一起）

```sql
kg_ingredient_request(
  id, food_key UNIQUE, raw_mention, normalized, requested_by,   -- anon:xxx 或 user_id
  hit_count INT DEFAULT 1, status, last_requested_at, created_at
)
kg_fetch_task(
  id, request_id FK, food_key UNIQUE, status,  -- queued|running|done|failed|rejected
  attempts, last_error, budget_day, started_at, finished_at, next_retry_at
)
kg_food_bundle(
  id, request_id FK, canonical_name, canonical_id, scientific_name, category, nature,
  score_total, score_detail JSONB, source_count, sources JSONB,
  hard_veto TEXT, status, reviewer, reviewed_at, reject_reason, created_at
)
kg_food_bundle_item(
  id, bundle_id FK, item_type,      -- node | rel
  field_name, field_value, source_span, source_url,
  rel, dst_name, dst_cid, evidence_type, evidence_level,
  grounded BOOL,                      -- §2.6 第 3 步结果
  item_status TEXT                    -- pending | passed | rejected
)
```

### 3.3 provisional + 检索排除

**节点加状态**：`Ingredient.status ∈ {active, provisional, rejected}`
**关系加状态**：`r.review_status ∈ {merged, auto_approved}`，且自动通过的必须在 bundle 里可回溯

**两道防线，缺一不可**：

1. **Neo4j Cypher**（`kg/query.py` 五套模板统一加）
   ```cypher
   WHERE r.evidence_type IN $allowed
     AND coalesce(b.status, 'active') = 'active'
   ```
2. **Milvus 实体索引**（`kg_entity` 集合）
   这一条最容易漏：`provisional` 节点如果进了向量库，实体链接照样能链到它，
   **然后 generate 就会拿未审核数据回答** —— Cypher 过滤根本管不到这条路径。
   做法：**实体只在审核通过后才写进 Milvus**（与 merge 同一步），从源头上就不存在。

**回答里的表现**：provisional 数据不进 `assemble` 的上下文，只在详情 API 里显示「待核实」。

### 3.4 异步与预算

```python
# 登记：同步、极快
request_id = kg.request.register(food_key, raw, who, ip)   # UPSERT，hit_count+1

# 触发：不阻塞响应
background_tasks.add_task(kg.fetch.run_once, request_id)
return {"status": "queued", "request_id": request_id}
```

必须处理的 5 个细节（这些是「上线才发现」的类型）：

| 细节 | 做法 |
|---|---|
| **进程重启丢任务** | `BackgroundTasks` 在进程内。启动时 recover：`UPDATE kg_fetch_task SET status='queued' WHERE status='running' AND started_at < now()-10min` |
| **并发重复抓** | CAS 抢占：`UPDATE ... SET status='running' WHERE id=%s AND status='queued' RETURNING id`，抢不到就什么都不做 |
| **负缓存** | `rejected` 的 food_key 7 天内不再抓；用户再问只回「该条目暂未收录」 |
| **日预算** | `SELECT count(*) FROM kg_fetch_task WHERE budget_day = current_date` ≥ 上限（默认 50）→ 只登记不抓 |
| **进度可查** | request 状态 + 预估时间，用户端轮询 |

---

## 4. 连带缺口

### 4.1 先澄清：这 4 条里有 3 条上一轮已经做完了

你引用的那段批评是我在**写方案之前**说的，现在代码已经不是那个状态：

| 批评 | 当前状态 | 证据 |
|---|---|---|
| `init_pg.py` 是 `CREATE TABLE IF NOT EXISTS` 堆叠、**没有迁移机制** | ✅ **已修（9.0）** | DDL 全部收编进 `migrations/002_business.sql` + `003_memory_vector.sql`；`init_pg.py` 变成兼容包装器，等价于 `migrate` |
| `api/` 目录**根本没建**，全堆在 `app.py` | ✅ **已修（9.0）** | 拆成 `api/{auth,admin,chat,health,system}.py`，`app.py` 从 192 行降到 71 行，只做装配 |
| **没有 `tests/`** | ✅ **已修** | `tests/` 29 项，远端 29/29 通过 |
| 评估集缺「未收录食材」这一类 | ✅ **已修（9.0）** | 追加 15 条 `reference=UNCATALOGUED`，总数 75（30 有据 / 30 应弃答 / 15 未收录）；新增 `uncatalogued_handling` 指标 |

所以 9.0 地基已全部落地，剩下的是 9.1-9.3 的业务实现。

### 4.2 迁移收编：把 `init_pg.py` 折进 migrations

```sql
-- migrations/002_business.sql（8 张业务表：dict_alias / dict_alias_candidate /
--   kg_candidate / session / message / user_profile / memory_fact + 索引）
```

两个必须注意的点：

**① `vector` 扩展不能由业务角色装**
pgvector 的 control 文件没有 `trusted = true`，`nutri`（即便是库 owner）执行
`CREATE EXTENSION vector` 会 `InsufficientPrivilege`。已实测。
做法：`002` 里保留 `CREATE EXTENSION IF NOT EXISTS vector`（已存在时是无操作），
并在 `scripts/migrate.py` 里对这一种错误给出**明确指引**而不是抛栈：

```
❌ 002_business.sql 执行失败：permission denied to create extension "vector"
   → 请先由超级用户执行：
     psql -U postgres -d <db> -c 'CREATE EXTENSION IF NOT EXISTS vector'
```

**② 顺序固定**：`migrate` → （超级用户装 vector，只需一次）→ `migrate` 重跑。
`migrate.py` 本身就是幂等的，重跑天然安全。

### 4.3 `api/` 剩余拆分

原则：**`app.py` 只做装配，不放业务逻辑**（挂中间件、include router、lifespan）。

```
api/chat.py     /chat（含 Langfuse trace 包裹）
api/kg.py       /kg/ingredient/{id} /kg/subgraph /kg/food/request
api/health.py   /health（四个存储探活）
```

`/auth` `/admin` 已经拆好了，照同样方式平移即可。`app.py` 预计从 ~180 行降到 ~40 行。

### 4.4 评估集：新增第 5 类样本

现有 4 类（有据搭配 / 有据禁忌 / 民间说法 / 应弃答），新增：

**「未收录食材」15 条**，期望行为不是弃答，而是三件事全对：

```jsonl
{"question":"黑豆怎么吃","expected_behavior":"uncatalogued",
 "must_say":["暂未收录","已记录"], "must_not":["营养数据","搭配建议","功效"]}
```

对应新增指标 **`UncataloguedHandling`**：三个子项各自打分
1. 是否明确说「暂未收录」（不能装懂）
2. 是否**没有**编造营养素/搭配/功效
3. 是否给出可执行的下一步（已登记 / 换个说法试试）

这与已有的 `AbstentionCorrectness` 是两件事：后者测「该拒答时拒答」，
前者测「该登记时登记，且登记前后都不编造」。

### 4.5 补一个你没提但更重要的指标：知识库覆盖率

```
覆盖率 = 1 - (未收录请求数 / 总请求数)
```

这个数字放在 `/admin/stats` 里，是判断「图谱该往哪长」的唯一依据：
- 覆盖率长期 95%+ → 补录 pipeline 可以降频，省预算
- 突然掉到 80% → 说明来了一波新用户群体（比如突然很多人在问西餐食材），该批量补

没有这个指标，Phase 9 就是「做了个功能」，有了它才是「建了个运营闭环」。

---

## 5. 执行顺序与工期

| 阶段 | 内容 | 工期 | 验收 |
|---|---|---|---|
| **9.0 地基** ✅ | 迁移收编 002/003/004、`api/` 拆完、eval 集加第 5 类 + `uncatalogued_handling` | ~~1 天~~ **已完成** | `migrate` 一次建全 14+ 表；`app.py` 71 行；pytest 29/29 |
| **9.1 登记链路** ✅ | `near_misses`、别名 vs 新食材分流、入口校验、复合词切分、request 表、限流、状态查询 | ~~1.5 天~~ **已完成** | pytest 59/59（连跑两遍可重复）；见 §5.2 实测 |
| **9.2 抓取与校验** ✅ | USDA 客户端、A 模型画像、同名异物核对、四维评分与硬否决、离线 worker | ~~2.5 天~~ **已完成** | 真实 API 产出 bundle；pytest 75/75；见 §8 |
| **9.3 审核闭环** ✅ | bundle 审核 CLI/API、节点候选表、转 `kg_candidate`、provisional 分层、别名回流、回滚 | ~~1 天~~ **已完成** | 审核前问「黑豆」→ 暂未收录；审核后 → 命中 6 条营养事实并标「单源」；pytest 88/88；见 §9 |
| 合计 | | **6 天全部完成** | |

### 5.1 9.0 实施记录（2026-10-27）

| 改动 | 说明 |
|---|---|
| `migrations/002_business.sql` | 原 `init_pg` 的 7 张业务表 + pg_trgm，与向量无关，任何 PG16 都能一次建成功 |
| `migrations/003_memory_vector.sql` | `memory_fact` 单独拆出：vector 扩展装不了时只影响这一张表，不牵连那 7 张 |
| `migrations/004_align_kg_candidate.sql` | **给老库补 `dst_name` 列** |
| `scripts/migrate.py` | 新增环境问题翻译（vector 无权限 / 镜像没有 pgvector / pg_trgm 无权限）；过滤 AppleDouble `._*.sql` |
| `scripts/init_pg.py` | 降级为兼容包装器，等价 `migrate` |
| `api/{chat,health,system}.py` | `/chat` `/health` `/llm/pool` 从 `app.py` 迁出；`app.py` 192 → 71 行 |
| `eval/dataset.jsonl` | +15 条 `UNCATALOGUED` |
| `eval/metrics.py` | 新增 `UncataloguedHandling`（确定性规则，不花 LLM） |
| `eval/run.py` | 新增 `--run-chain`；不传时若 response 为空会明确警告 |

**两个过程中发现的真问题**

1. **`kg_candidate` 缺 `dst_name` 列**（老库）。`CREATE TABLE IF NOT EXISTS` 对已存在的表不会加列，
   而 `kg/review.py` 和 `api/admin.py` 的 SELECT 都用到它 —— 老库跑完 002 仍然报错。
   这就是为什么必须有 `004`：**给已存在的表加列只能靠新的 ALTER 迁移，不能回头改已应用的迁移**
   （否则不同环境结构会分叉）。

2. **评估链路一直在评空回答**（重要）。`eval/run.py` 的 docstring 写着「对每条跑链路拿到
   answer + contexts」，但代码只是读 jsonl 直接评估，而 dataset 里 `response` 是空串。
   也就是说之前跑出来的 Faithfulness / Relevancy 评的是空字符串，**分数没有意义**。
   已加 `--run-chain`（用真实链路填 answer 与 contexts）并在不传时明确警告。
   → 简历上如果要写评估数字，必须用 `--run-chain` 跑一遍，否则面试官一问就露。

**顺手修的两个小坑**

- 4 字节 emoji（💡）在部分终端/locale 下显示成乱码，CLI 提示改成 `[需人工处理]`
- macOS 打包产生的 `._xxx.sql` 会被 `glob("*.sql")` 命中并当迁移读，报 `UnicodeDecodeError` 且行号无意义；迁移器现在跳过 `.` 开头的条目

**验证**：远端 Python 3.14.8 + PG16，`migrate --status` → 001~004 全部已应用；
`pytest -q tests` → **29 passed**；在全新空库上跑迁移可复现「002 成功提交、003 给提示并停下」的行为。

---

## 6. 待你决策

1. **数据源范围**：只接 USDA（有 API、稳、但没有性味和中式叫法）／只接中国官方平台（有别名和英文名，但要爬 HTML 且不可商用）／**两者都接（推荐）**？
2. **抓取合规底线**：中国食物成分表数据「供免费查阅」，但版权属国家基础资源，**商用需授权**。这个项目是练习/答辩还是要对外？决定能不能爬。
3. **`provisional` 期间给用户看什么**：只说「暂未收录」／ 给向量降级结果并标注未核实（有体验但风险）？
4. **审核人力**：bundle 审核打算谁来点？纯 CLI 够不够，还是需要最小 Web 页（约 +1 天）？
5. **`kg_ingredient_request` 的归属**：用匿名身份还是必须登录？匿名可减少摩擦，但会给刷词留下入口。

---

## 7. 9.1 实施记录（2026-10-27）

### 7.1 交付内容

| 文件 | 内容 |
|---|---|
| `kg/textsim.py` | 中文相似度：包含关系 + 字级编辑距离（替代对中文失效的 pg_trgm） |
| `kg/normalize.py` | `LinkResult` 加 `residual` / `near_misses`；新增 `link_question()` 字典最长匹配；`AliasStore.fuzzy_lookup()` + 全量别名进程内缓存 |
| `kg/request.py` | 三条出口（非食材 / 疑似别名 / 新食材）、复合词归一、候选词抽取、幂等 UPSERT、双维度限流、状态查询 |
| `migrations/005_food_request.sql` | `kg_ingredient_request`（`food_key` 唯一 + `hit_count` 热度） |
| `graph/nodes.py` | `link_node` 改用字典最长匹配；新增 `uncatalogued_node`；`route_after_link` 三路分流 |
| `graph/workflow.py` | 挂 `uncatalogued` 节点（→ `memory_write`，不过 generate/verify） |
| `api/kg.py` | `POST /kg/food/request`、`GET /kg/food/request/{id}` |
| `tests/test_food_request.py` | 30 项（含整条链路 `/chat` → uncatalogued） |

### 7.2 实测行为（脚本跑在真实库上，跑完已清数据）

```
Q: 黑大豆和什么一起吃好？
   命中实体     : []
   未收录候选   : ['黑大豆']
   → alias_candidate  「黑大豆」没有直接收录，但你说的可能是「黑豆」？

Q: 痛风能吃黑豆吗？
   命中实体     : [('Ingredient','黑豆'), ('Population','痛风')]
   未收录候选   : []

Q: 阿司匹林能和黑豆一起吃吗？
   命中实体     : [('Ingredient','黑豆')]
   → new_request  key=阿司匹林（进待审，人工驳回后进负缓存，不再重复抓）

Q: 红烧肉配什么好？
   未收录候选   : ['红烧肉']
   → new_request  key=红烧肉
```

### 7.3 三个实打实的问题（都是验证时才暴露的）

**① `pg_trgm` 对中文完全是摆设**

实测 `similarity()` 全为 0：

| 对比 | pg_trgm | 字级相似度 |
|---|---|---|
| 黑大豆 / 黑豆 | **0.000** | 0.667 |
| 黑豆粥 / 黑豆 | **0.000** | 0.667 |
| 西红柿子 / 西红柿 | **0.000** | 0.750 |
| 鹰嘴豆干 / 鹰嘴豆 | **0.000** | 0.750 |

**这意味着原方案里「trigram 0.60~0.85 判别名」这条判据根本不会触发**，必须换成
`kg/textsim.py` 的包含关系 + 编辑距离。`trigram_lookup` 保留但只在英文名/拼音场景用。

**② 实体链接原来只有「精确匹配」这一级（更严重）**

旧 `link_node` 拿**整句**去 `exact_lookup` —— 而整句永远不会等于某个别名，
所以对自然语言提问 100% 失效，只有用户正好只打一个词时才命中。
再加上 L3 向量根本没接线（`EntityLinker(alias_store)` 没传 `vector_search`），
所谓「四级漏斗」实际是**一级**。

改成字典最长匹配后，「痛风能吃黑豆吗」能同时切出 `Ingredient:黑豆` + `Population:痛风`
（旧实现这里 0 命中）。这一条比 9.1 本身更有价值。

**③ 复合词切分会造伪食材**

「红烧肉配什么好」→ 切出「红烧肉配」→ 剥掉前缀「红烧」→ 剩「肉配」→ 通过长度校验 → 被登记。
两次修正：
- `_BREAK_WORDS` 补上 `配 / 搭 / 合适 / 好处 / 做法`（实测漏词）
- `normalize_food_term` 剥离后做二次校验：残留**强停顿词**就放弃剥离。
  注意只能用「强停顿词」（多字 + 配/搭），单字虚词（好/是/多/不）会把「多宝鱼」「无花果」误伤，
  导致剥离全部变成空操作。

### 7.4 已知能力边界（不是 bug，但要写清楚）

- **「阿司匹林」能进待审**。纯规则判断不了「这是药不是食物」，它会占用一条待审记录，
  人工驳回后进负缓存（7 天不再抓），成本可控且自愈。要做真分类得加一次 LLM 判定（9.2 或之后）。
- **一句里既有已收录食材又有未收录食材时，只答已收录的那部分**（例如「黑豆和西红柿能一起吃吗」
  会答西红柿，黑豆不登记也不提示）。修它要改 `assemble`/`generate` 的上下文拼装，
  放 9.3 一起处理。
- `AliasStore` 的全量别名缓存是**每实例**的，`link_node` 每次新建实例 → 每次问答仍是一次全表查询。
  字典到几千条后应改为进程级单例 + 主动失效。

### 7.5 测试卫生

初轮 59 项里 2 项在**第二次运行**时挂掉：用例往 `dict_alias` 插了「黑豆」但没清理，
再跑时「黑豆」已是 catalogued。已加 autouse fixture，只删 `ING_TEST_*` / `POP_TEST_*` 前缀的
测试数据，不动真实数据。现在连跑两遍都是 59/59。

---

## 8. 9.2 实施记录（2026-10-27）

### 8.1 交付内容

| 文件 | 内容 |
|---|---|
| `kg/usda.py` | FDC 客户端（search / get_food）、按 `nutrient.id` 解析、两种 foodCategory 形态归一 |
| `kg/draft.py` | 「A 模型」：中文食材 → 检索画像（`fdc_query` + `identity_keywords` + `exclude_keywords` + 学名）；内置 5 条静态画像可离线跑 |
| `kg/verify.py` | 四维加权 + 硬否决 + 自动通过判定，**不调用模型给自己打分** |
| `kg/fetch.py` | 流水线：CAS 领取 → 画像 → 检索 → 同名异物核对 → 详情 → 校验 → 落库；含重试/恢复/预算 |
| `migrations/006_food_bundle.sql` | `kg_fetch_task` / `kg_food_bundle` / `kg_food_bundle_item` |
| `scripts/run_fetch.py` | worker CLI（`--limit` / `--task-id` / `--recover` / `--status`） |
| `kg/request.py` | 登记成功后自动排队一个抓取任务（`ON CONFLICT DO NOTHING`，重复登记不重复消耗预算） |
| `tests/test_fetch.py` | 16 项（含同名异物、评分边界、流水线幂等、重试与恢复、预算） |

### 8.2 真实端到端结果（真调 USDA，不是 mock）

```
1) 用户问「黑豆」未收录 → new_request  food_key=黑豆  id=57
2) 抓取：done  命中=Soybeans, mature seeds, raw
   评分=0.88  产物状态=pending_review  营养素=7 项
3) 产物 bundle
   中文名=黑豆  英文名=soybeans  学名=Glycine max
   分类=Legumes and Legume Products  fdcId=174270
   评分=0.88  {completeness:1.0, authority:1.0, agreement:0.6, no_conflict:1.0}  来源数=1
4) 逐项
   [nutrient] 能量      446.0 kcal  ← fdc:174270#1008
   [nutrient] 蛋白质     36.49 g     ← fdc:174270#1003
   [nutrient] 脂肪      19.94 g     ← fdc:174270#1004
   [nutrient] 碳水      30.16 g     ← fdc:174270#1005
   [nutrient] 膳食纤维    9.3 g      ← fdc:174270#1079
   [nutrient] 钠        2.0 mg      ← fdc:174270#1093
5) request=pending_review   task=(done, 1)
```

数值与《中国食物成分表》黑豆（干）量级一致（蛋白质 36%、脂肪 20%），说明**选对了物种**。

### 8.3 关键设计：校准锚点必须是「物种独有的词」

原方案写的是「学名 / 分类不一致 → 硬否决」。实测发现**分类这条路走不通**：

```
黑豆（Glycine max，大豆属）  → foodCategory: "Legumes and Legume Products"
黑菜豆（Phaseolus vulgaris） → foodCategory: "Legumes and Legume Products"   ← 完全一样
```

USDA 对 SR Legacy 记录**不返回 `scientificName`**（实测为 None），也没有其他物种字段可依。

所以判据改成两个词表，由「A 模型」产出：

| 字段 | 作用 | 黑豆的值 |
|---|---|---|
| `identity_keywords` | 描述里**必须**出现的物种独有词 | `soybean` |
| `exclude_keywords` | 出现即否决的同名异物词 | `black` / `turtle` / `mung` / `adzuki` … |

这样即使模型把「黑豆」翻成 "black beans"（检索会一击命中错误条目），
`identity_keywords=soybean` 仍然会把 "Beans, black, mature seeds, raw" 拦下。

### 8.4 刻意的保守：单源永远进人工

一致性权重 0.30，单源取 0.6 → `0.20 + 0.30 + 0.18 + 0.20 = 0.88 < 0.9`。
**所以当前所有 bundle 都进 `pending_review`**，自动通过通道实际是关着的。

这不是没做完，而是刻意的：只有一个来源时无法交叉验证，凭一个来源自动写进主图
就是在赌 API 没有同名异物。接入第二个来源（中国食物成分表离线导入）后，
同样的代码会自然放开自动通过 —— 逻辑已经写好并有单测覆盖（`source_count=2 → 1.0 → approved`）。

### 8.5 关于 grounding 的诚实说明

方案里说「A 抽字段要带 `source_span`，B 判断蕴含」。**在结构化 API 上这句话要打折**：

- 营养素是 API 直接给的数值，没有「散文正文」可以做语义蕴含
- 所以可追溯性实际退化为「每个数值都能指回原始响应」：`source_locator = fdc:174270#1003`
- 真正的 span 匹配要等接网页源（中国食物成分表 HTML）才有意义

这一点写在 `kg/verify.py` 的模块注释里，避免以后有人以为这里做了语义校验。

### 8.6 两个刻意的取舍

**① 抓取做成离线 worker，不用 FastAPI BackgroundTasks**
`BackgroundTasks` 活在进程内：进程重启任务就丢了，还得额外做恢复。
做成 `python -m scripts.run_fetch` 之后：可手动重跑、可挂定时任务、可单步调试，
也符合「抓取是建库动作，不该挂在用户请求上」的分工。（`recover_stale()` 仍然保留，
因为将来真上后台线程/容器时它是必需的。）

**② 性味（寒热温凉）不由模型生成**
USDA 和中国食物成分表都没有这个维度。让模型凭记忆填一个「性平」就是编造，
所以 `kg_food_bundle.nature` 一律留空，等药食同源目录离线导入。

### 8.7 已知边界

- **`DEMO_KEY` 只有 30 次/小时、50 次/天，且全站共享**。实测跑几个食材就撞 429。
  生产/演示前必须换成自己的 key（`api.data.gov` 免费申请，1000 次/小时）。
  429 会被识别为「可重试」并按 0.5h×attempts 退避，不会把任务打死。
- **`FOOD_DRAFT_MODE=llm` 这条路径尚未用真实模型验证**（远端没有 DASHSCOPE_API_KEY）。
  目前默认 `auto`：内置画像优先，没有再调模型。要验 LLM 路径需要一个能用的 key。
- 只接了一个来源，所以「多源一致性」这一维恒为 0.6。
- `kg_food_bundle` 与 `kg_ingredient_request` 是 `UNIQUE(request_id)`：
  同一请求重跑会**覆盖**旧产物，历史版本不留存。要审计留痕需要加版本表（9.3 可选）。

### 8.8 验证

远端 Python 3.14.8 + PG16：`pytest -q tests` → **75 passed**；
真实 USDA 调用产出 bundle（见 §8.2）；`run_fetch --status` 显示
`抓取任务 {'done': 1} / 产物 {'pending_review': 1} / 今日预算 1/50`。跑完已清空演示数据。

## 9. 9.3 实施记录（2026-10-27）

### 9.1 数据流：审核只做「翻译」，写图只有一个入口

```
kg_food_bundle (pending_review)
   │  approve
   ├─→ kg_node_candidate  待建节点（Ingredient + 图里还没有的 Nutrient）
   └─→ kg_candidate       待建边（CONTAINS_NUTRIENT）
         │  kg.merge（唯一写入口：先节点、后边）
         ├─→ Neo4j 主图
         ├─→ dict_alias   ← 必须在 merge 之后，否则「能链到 cid 但图里没节点」
         └─→ bundle=merged / request=approved
```

为什么加一张 `kg_node_candidate`：bundle 是整块画像，主图里却是「节点 + 边」两类东西。
原来只有边候选表，新食材没有地方表达「要新建一个 Ingredient」，结果只能是
某个脚本偷偷写节点或手工 Cypher —— 一旦有两个写入口，「这条边是谁加的、
哪个批次、能不能整批回滚」就永远说不清。补上节点候选后，`kg.merge` 是唯一入口。

### 9.2 一个被推翻的方案：provisional 不是「藏起来」

原方案写的是「新数据打 provisional 且**检索时排除**」。实现时发现这么写会自相矛盾：

- 审核通过了却查不到 → 用户以为还是「未收录」，转头又登记一次（9.1 刚花力气避免的重复）
- 真正的风险不是「它在图里」，而是「它被当成硬证据引用」——单一来源的数值，
  未经交叉验证，却和「两份资料互证」的数据长得一模一样

所以改成语义更准确的版本：

| 概念 | 含义 | 处理 |
|---|---|---|
| 人工审核 | 「这是不是那个东西」 | 没通过就**根本不进主图**（最硬的隔离） |
| 交叉验证 | 「这个数对不对」 | `source_count < FOOD_TRUSTED_MIN_SOURCES` → 边打 `provisional` |

`provisional` 的边：证据等级封顶 C（多源才给 B），上下文标 `[measured/C·单源]`，
回答前缀换成「单一来源补录，未做多源交叉验证」——**不冒充「民间说法」**，
那是另一回事（`WEAK_DISCLAIMER` 说的是证据类型，这里说的是验证程度）。

同时保留 `INCLUDE_PROVISIONAL=false` 的严格模式（Cypher 里是真的过滤，
有测试断言严格模式返回 0 条），给「只答硬证据」的场景用。

### 9.3 三个必须做对的细节

1. **同名不新建**：审核时先查 `dict_alias`，再查「已审核但未入图的节点候选」，
   都没有才 `nextval` 分配新 id。所以「黑大豆」已经存在时会挂过去（`identity=existing_alias`），
   而不是造出第二个黑豆节点 —— 这是 9.1 的 near_miss 在最后一步不被破坏的前提。
2. **canonical_id 分配幂等**：`ingredient_cid_seq` 从 1000 起步（种子是 1~125，
   一眼能看出「这是补录进来的」）。先查后分配，`merge` 失败重试不会留下孤儿节点。
3. **别名绝不抢**：某个名字已挂在别的 canonical_id 上就跳过并记日志。
   宁可新食材查不到，也不能让同一个名字指向两个节点。

### 9.4 实测（远端 PG16 + Neo4j 5.26 + Python 3.14.8）

```
0. 问「黑豆含什么营养？」→ 命中实体 []            ← 种子 50 个食材里没有黑豆
1. 登记 → action=new_request  request_id=111
2. 抓取 → done，命中「Soybeans, mature seeds, raw」，评分 0.88，7 项营养素
3. 审核前再问 → 命中实体 []                        ← 还没进图，确实查不到
4. approve → identity=created  ING_01017
             节点 3（Ingredient + 能量 + 钠）/ 边 6（能量 kJ 被跳过）
             别名写入：黑豆, soybeans
5. 审核后再问 → 命中 Ingredient/黑豆/ING_01017
   默认（含单源）：6 条 [measured/C·单源]
   严格（只答交叉验证）：0 条
6. 再问「黑大豆」→ alias_candidate，建议挂到 ING_01017，**不新建**
```

`pytest -q tests` → **88 passed**（新增 13 项：翻译、幂等、身份判定、状态机、provisional 检索）。

> 抓取这一步用的是**回放客户端**：DEMO_KEY 只有 30 次/小时且全站共享，这次已经 429。
> 回放的每条数值都来自上次真实调用（fdcId 174270），
> `choose` 的物种核对 → `parse_nutrients` → `verify` 打分 → 落 bundle 全部走真实代码。

### 9.5 期间发现的两个真 bug（与 9.3 无关，但会挡住整条链路）

1. **Neo4j 属性不支持 map —— 种子数据 40 条边一条都写不进去。**
   `scripts/load_seed.py` 把 `sources` 当 `[{...}]` 直接 `SET r.sources = $sources`，
   报 `CypherTypeError: Property values can only be of primitive types or arrays thereof`。
   也就是说 **Neo4j 一直是空的**（125 个节点都在、0 条边），图谱查询永远返回空，
   任何「有据回答」都不可能发生。已在 `kg/schema.py` 加 `sources_to_json()`，
   图里统一存 JSON 字符串（PG 侧仍是 JSONB），`kg/merge.py` 同步修正。

2. **`.env` 的 Neo4j 密码与服务器实际不一致**：`.env` 写 `nutrigraph-dev`，
   而 `/opt/db/.env` 与容器实际是 `Neo4j@2026`。已在 `.env` / `.env.example` 对齐。
   这是继「PG 连不上 / 账号不存在 / 镜像缺 pgvector」之后**第四个环境不一致**，
   共同点是「配置文件写的是理想状态，服务器是另一个状态」。

### 9.6 发现的一处数据问题

`痛风` 同时存在于 `symptom.csv`（SYM_00011）和 `population.csv`（POP_00004 痛风患者），
所以同一个词会链到两个实体，而「是人群还是症状」的提示原来取决于 SELECT 返回顺序
—— 用户看到的回答会随机变。已在 `kg/request.py` 加固定优先级
（Population > Taboo > Nutrient > Symptom），
理由：用户问「痛风能吃吗」，需要听到的是「痛风是疾病不是食材」，而不是「痛风是个症状」。

要不要把 `痛风` 从 Symptom 里去掉，是数据口径问题，需要你定：
它在「关节红肿热痛」的语境下确实是个症状，在「痛风患者忌口」的语境下是人群。

### 9.7 已知边界

- **性味仍然为空**：USDA / 食物成分表都没有这个维度，模型填一个「性平」就是编造。
  等药食同源目录离线导入。
- **`amount` 属性没落边**：数值写在 `claim_text` 里（`黑豆每 100g 含蛋白质 36.49 g`），
  和种子边的存法一致。要把 amount/unit 做成独立属性，需要给 `kg_candidate` 加两列。
- **Milvus 未接线**：`kg_entity` 集合不存在，实体链接的向量兜底自然跳过。
  所以「provisional 不写 Milvus」这条暂时是空保证 —— 等接线时必须在
  `promote.merge_pending` 的别名回流旁边加索引写入，且只索引 `merged` 的节点。
- **审核界面是 CLI**：`api/admin.py` 的四个接口已经就绪，做页面随时能接。

## 10. 部署同步与真机验收（2026-10-27 12:20）

把本地代码同步到服务器 `/opt/code/nutri_graph` 并跑完整流程。同步前的部署快照是
**比 Phase 5 还早的版本**：没有 `api/`、`security/`、`migrations/`、`tests/`，**连 `llm/` 都没有**。

### 10.1 同步方式

| 项 | 做法 |
|---|---|
| 整目录备份 | `/opt/code/backup/nutri_graph.<ts>.tgz` + `env.<ts>`（保留 `.env` 原件） |
| 代码 | tar 覆盖，**排除 `.env`**（服务器上那份才有真实的 `DASHSCOPE_API_KEY` 和 `LLM_MODEL_POOL`） |
| 环境变量 | 只补不改：已有值一律不动，缺失的 45 个键按 `.env.example` 补齐 |
| 三个密钥 | `JWT_SECRET` / `PHONE_ENC_KEY` / `PHONE_HASH_PEPPER` 在服务器上现生成 —— 本地那份是调试用的，两边共用同一把反而更差 |
| 纠正 | `NEO4J_PASSWORD` 由 `nutrigraph-dev` 改为实际的 `Neo4j@2026`；删掉 config 已不读的 `LLM_QUOTA_COOLDOWN_H` |

补齐后与 `config.py` 严格对齐：**89 / 89 / 89**（config = `.env` = `.env.example`）。

### 10.2 真机跑通的流程

```
/health            503 degraded（Milvus 没起，PG/Neo4j 正常 —— 如实报告，不假装健康）
/auth/sms          200，DEBUG=false 时验证码只在日志里（真实部署本该如此）
/auth/token        200 → 138****9389
/chat 痛风能吃海鲜吗     6.5s  graph_hit=True  证据 {A:2}   「痛风患者慎食高嘌呤海鲜」
/chat 西红柿和什么一起吃  4.0s  graph_hit=True  证据 {A:1,B:1} 「西红柿可以和鸡蛋、豆腐一起吃」
/chat 黑豆（未收录）      「暂未收录…已记录（编号 #293）」+ 结构化 food_requests
抓取 → 审核 → 合并        bundle 133 / ING_01075 / 3 节点 6 边 / 别名 黑豆+soybeans
/chat 黑豆含什么营养      graph_hit=True  {C:6}  措辞「单一来源实测值，未交叉验证」
/chat 黑豆和什么一起吃     graph_hit=False        「已经收录，但还缺这方面的证据」
```

最终三处一致（PG bundle / Neo4j / dict_alias 全部指向 `ING_01075`）：

```
kg: {"pending_candidates":0,"pending_bundles":0,
     "food_requests":{"approved":1,"total":1,"resolution_rate":1.0}}
图规模 128 节点 / 46 边   （种子 125/40 + 黑豆 + 能量 + 钠）
pytest：默认 90 skipped（门禁）｜ TEST_DB_OK=1 → 90 passed
```

### 10.3 这轮抓出的 4 个问题

**① `llm/` 目录从来没同步过。** `/chat` 走到 `generate` 就 `ModuleNotFoundError: No module named 'llm'`。
也就是说这个部署快照**从来没有真正生成过回答**——之前只是"服务能起来"。
这类问题只有真跑一次 HTTP 才会暴露，看目录列表看不出来（缺的是一个不显眼的子目录）。

**② embedding 一直被 DashScope 拒绝（400），错误信息极误导。**
`langchain-openai 0.3.34` 默认 `check_embedding_ctx_length=True`，会先用 tiktoken 把文本
切成 token 数组再发 `input=[[...]]`。OpenAI 官方接受，DashScope 兼容端点不接受：

```
400 InternalError.Algo.InvalidParameter:
Value error, contents is neither str nor list of str.: input.contents
```

直接用 httpx 发 `input=["黑豆"]` 是 200 —— 所以不是 key、不是模型名、不是维度。
`rag/embed.py` 加一行 `check_embedding_ctx_length=False` 后正常返回 1024 维。
**这条同时影响**：向量降级、Milvus 实体索引、Ragas 评估的 embedding。

**③ 降级失败直接变成 HTTP 500。**
「黑豆和什么一起吃好」→ 图里黑豆只有营养素边、没有搭配边 → 走 `vector_fallback` →
Milvus 没起 + embed 400 → 异常冒到 HTTP。已改成：**降级也失败 = 无依据弃答，不是 500**。
用户该看到「我没找到依据」，而不是一个空白页。

顺带把两种"没答案"分开：
- 不认识的食材 → 登记（原有逻辑）
- **认识但没有这方面的证据** → 「「黑豆」已经收录，但我暂时没有能回答这个问题的确切依据」
  都回一句"没找到依据"的话，用户会以为食材没收录，转头又登记一次。

**④ 单源数据的 prompt 文案把实测值说成"民间说法"。**
`TIER_WEAK` 的规则原文是「只有传统经验或民间说法（C/D 级）」，模型照着复述成
「属于传统经验或民间说法级别」。可 USDA 的 36.49g 蛋白质是**实测值**，
问题在校验程度、不在证据类型。已给 `build_grounded_prompt` 加 `provisional` 分支，
换成「单一来源实测、未做多源交叉验证、仅供参考」。

### 10.4 测试与部署同库的坑（已加门禁）

部署库是有数据的。pytest 的 fixture 会 `TRUNCATE` 业务表，但**清不到 Neo4j 和 `dict_alias`**，
于是跑完测试的结果是：图里「黑豆」这个节点好好地在回答，`/admin/stats` 的已合并产物却是 0
—— 「谁、什么时候、凭什么审核进来的」全没了。这正好踩中补录设计最不能破的一条。

已加 `tests/conftest.py::_destructive_guard`：库名不含 `test` 就整组跳过，除非显式 `TEST_DB_OK=1`。
本次踩到的孤儿数据（`bundle:63` 留下的 6 条边、无 bundle 记录的 `ING_01034`）已回滚清理，
然后**重新走了一遍补录**，让三处重新对上。

### 10.5 顺带修掉的 API 缺口

`/chat` 一直没返回 `graph_hit` 和 `food_requests`（`_food_requests` 在 state 里有，被 API 丢掉了）。
前端只能从「已记录并提交给知识库管理员（编号 #293）」这句话里正则抠数字。已补上。

### 10.6 现在的部署状态与注意事项

- 服务**没有常驻**（本次是前台起、验完即停），也没有 systemd unit。
  要对外提供访问得单独做，并且**先开 `AUTH_REQUIRED=true` 或加反代限流** ——
  现在 `/chat` 是免登录的，等于把带 API Key 的模型调用开放给任何能访问 8810 的人。
- `SMS_PROVIDER=console`：不真发短信，验证码只进日志。真上线要换 `dypns`（见 §1.3）。
- `DEBUG=false`（正确）—— 如果是 true，`/auth/sms` 会把验证码回填到响应里，
  等于**任何人都能以任意手机号登录**（拿到别人手机号的码直接换令牌）。
