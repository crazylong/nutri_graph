# nutri_graph —— 食疗养生助手 · 系统设计方案与执行计划

> 输入：`needs.txt`
> 参考：`msb_assistant`（工程骨架 / LangGraph / 记忆）、`gme_rag`（Ragas）、`Nutrigraph`（证据分层 / crosswalk / 图浏览）
> 技术栈：LangChain · LangGraph · Milvus · Neo4j · PostgreSQL · DashScope · FastAPI

---

## 0. 一页速览

一句话定位：**用一张带证据等级的食疗知识图谱去约束 LLM，让"菠菜豆腐不能同吃"和"西柚影响他汀代谢"在系统里不是同一种东西。**

三条主线，其它都是支撑：

| 主线 | 解决的问题 | 核心设计 |
|---|---|---|
| **可信** | 模型不臆造 | 证据分层（type × level）+ 只按图事实生成 + 无据时明确弃答 |
| **准确** | 口语能落到图上 | crosswalk 归一 + 关键词/向量双路实体链接 + 口语症状映射 |
| **可运营** | 图谱能持续长大且不脏 | 抽取→候选表→人工确认→UNWIND 幂等 MERGE |

差异化卖点（面试/答辩可讲的一句话）：
> 不是"又一个 GraphRAG"，而是**给每条养生建议标注证据来源和强度**，检索时可按证据等级过滤，回答时能给出依据分层。

---

## 1. 需求解构

### 1.1 需求条目 → 设计含义

| # | needs.txt 原文要点 | 设计含义 | 关键实现点 |
|---|---|---|---|
| 3 | 5 类节点 / 5 类边；「不宜同食」带 evidence_level 和来源 | 关系是一等公民，**边上有证据，不是边上有备注** | 边上统一注入 `evidence_type` / `evidence_level` / `sources[]` / `claim_text` |
| 3 | 民间说法 vs 有依据的禁忌分开存 | **两个正交维度**：证据种类（type）+ 证据强度（level） | `traditional / anecdotal` 与 `guideline / measured` 分级，检索可 `WHERE r.evidence_type IN $allowed` |
| 4 | 别名归一（西红柿 / 番茄不能是两个节点） | 归一是**数据问题**，不是 if-else 问题 | crosswalk + `dict_alias` 表 + 唯一约束，见 §2.3 |
| 4 | LLM 抽关系 → 候选表 → 人工确认 → MERGE 主图 | 抽取结果**永不直写主图** | PG `kg_candidate` 表 + review 状态机 + 批次号 |
| 4 | UNWIND 批量 MERGE；源+类型+目标 建唯一约束；重跑不重复 | 幂等写入 | ⚠️ Neo4j **不支持关系唯一约束**，见 §2.4 修正方案 |
| 5 | 关键词+向量实体链接；口语→症状 | 实体链接是**召回瓶颈**，决定上限 | Milvus `kg_entity` 集合 + alias 精确匹配前置 |
| 5 | 按意图选跳数 | 不同问题走不同 Cypher 模板 | 搭配 1 hop / 忌口 2 hop / 替代 1 hop + 营养素排序 |
| 5 | 结果连同 evidence_level 进上下文，只按图事实答 | 上下文**结构化**而非拼文本 | 每条事实序列化为 `{s, r, o, evidence, source}` |
| 6 | 剂量/做法/适用条件只存属性、只回摘要 | 节点属性**冷热分离** | `summary`（≤40 字，进上下文）/ `detail`（长文，仅详情页） |
| 6 | 无命中降级纯向量，明确答"没有找到确切依据" | **弃答是一等公民** | `graph_hit=0` → vector fallback → 仍低分则固定弃答话术 |
| 8 | 长短期记忆存 PostgreSQL | 不引入 Redis，统一 PG | `message` / `session` / `user_profile` / `memory_fact`(pgvector) |
| 9 | Ragas + Langfuse | 评估与追踪要**接在真实链路上**，不是离线脚本 | LangGraph callback → Langfuse；Ragas 分数回写为 trace score |

### 1.2 needs.txt 没写、但必须有（否则项目跑不起来）

1. **安全合规**：医疗免责 + 红旗症状（胸痛/呼吸困难/意识改变/孕产急症）绕过图谱直接建议就医。
2. **过敏/慢病硬约束**：这是记忆系统真正的价值锚点 —— 用户"海鲜过敏""痛风"，必须在 Cypher 里做排除，而不是写进 prompt 求模型注意。
3. **关系可审计**：每条边记录 `created_by / review_status / batch_id / source_locator`，否则出问题无法回溯。
4. **冷启动数据**：图谱从哪来？见 §5.1，这是本项目最大风险。
5. **多轮指代消解**："那豆腐呢？"需要 last_entity 补全，否则实体链接必崩。
6. **中文别名覆盖**：地域叫法（西红柿/洋柿子/番茄）、错别字、简称。alias 表要能**回流增长**。
7. **成本可控**：LLM 抽关系是主要开销，要切片 + 去重 + 增量抽。
8. **输出可引用**：用户能看到"这条建议出自《中国居民膳食指南(2022)》第 X 页"。

---

## 2. 核心设计

### 2.1 图谱 Schema

**节点（5 类）**

| Label | 说明 | 关键属性 |
|---|---|---|
| `Ingredient` | 食材 | `canonical_id`(ING_00001) `canonical_name` `aliases[]` `category` `nature`(性味) `summary` `detail` `evidence_profile` |
| `Nutrient` | 营养素 | `canonical_id`(NUT_...) `name` `unit` `category` |
| `Population` | 人群 | `canonical_id`(POP_...) `name` `kind`(group/disease) |
| `Symptom` | 症状 | `canonical_id`(SYM_...) `name` `aliases[]` `tcm_term`(上火/湿热…) |
| `Taboo` | 禁忌对象 | `canonical_id`(TAB_...) `name` `taboo_type`(药物/疾病状态/烹饪方式/极端搭配) |

**关于「禁忌」节点的一个建模澄清（需你确认）**

needs.txt 里"人群"和"禁忌"语义有重叠。建议这样切分，让五类各司其职：

- `Population` = **谁**：孕妇、婴幼儿、老年人、痛风患者
- `Taboo` = **禁忌的那件事**：他汀类药物、高尿酸血症、空腹大量食用、生豆浆

这样需求里的两类禁忌天然分家：

```
「痛风慎食高嘌呤」  →  (海鲜)-[:CONTRA_POPULATION]->(痛风患者)         evidence: guideline/A
「西柚影响他汀代谢」→  (西柚)-[:CONTRA_ITEM]->(他汀类药物)              evidence: guideline/A
「菠菜豆腐不能同吃」→  (菠菜)-[:INCOMPATIBLE_WITH]->(豆腐)             evidence: traditional/D
```

前者连人群、后者连禁忌对象，第三种才是食材对食材。**民间说法和医学禁忌在结构上就不同**，不只靠属性区分 —— 这比单靠 `evidence_level` 更彻底。

**边（5 类主 + 2 类辅助）**

| 边 | 方向 | 属性 |
|---|---|---|
| `CONTAINS_NUTRIENT` 含有营养素 | Ingredient→Nutrient | `amount` `unit` `per`(=100g) |
| `RECOMMEND_PAIR` 推荐搭配 | Ingredient↔Ingredient | `effect` `rationale` + **证据四件套** |
| `INCOMPATIBLE_WITH` 不宜同食 | Ingredient↔Ingredient | `reason` `severity` + **证据四件套** |
| `CONTRA_POPULATION` 人群禁忌 | Ingredient→Population | `condition`(急性期/长期) + **证据四件套** |
| `SUBSTITUTE_FOR` 替代食材 | Ingredient→Ingredient | `basis`(营养素相似/功效相近) `similarity` + 证据 |
| `CONTRA_ITEM` 禁忌事项（辅助） | Ingredient→Taboo | `interaction`(影响吸收/加重代谢负担) + 证据 |
| `HAS_SYMPTOM` 关联症状（辅助） | Population→Symptom | `relation`(易出现/加重) |

**证据四件套（所有关系统一注入）**

```
evidence_type : guideline | measured | traditional | anecdotal
evidence_level: A | B | C | D
sources       : [{type, title, publisher, year, url, locator}]
claim_text    : 原始断言文本（用于引用和回溯）
```

`type` 和 `level` **正交**：type 说"这是哪一类证据"，level 说"多可信"。

| level | 含义 | 典型来源 |
|---|---|---|
| A | 指南/临床共识/权威机构警示 | 中国居民膳食指南、中国药典、FDA 葡萄柚警告 |
| B | 实验测定/人群研究 | 中国食物成分表实测值、RCT |
| C | 传统典籍/药食同源目录 | 本草纲目、卫健委药食同源目录 |
| D | 民间流传/网络说法（未验证） | 食物相克传言 |

**Ingredient 级证据画像（对应 12.a 的 provenance 传递）**

构建完成后为每个食材聚合写入 `evidence_profile`：

```json
{"total": 18, "by_type": {"guideline": 3, "measured": 9, "traditional": 5, "anecdotal": 1},
 "strongest_level": "A", "contested": 2}
```

`contested` = 同一对食材既有推荐又有禁忌（矛盾关系计数）—— 这个字段本身就是产品亮点：**能主动告诉用户"这条说法存在争议"**。查询时直接返回，不用每次现场聚合。

### 2.2 检索时的证据策略

| 场景 | 默认 allowed evidence_type |
|---|---|
| 人群禁忌 / 药物相互作用 | `guideline, measured`（严格） |
| 推荐搭配 | `guideline, measured, traditional` |
| 用户切"看看民间说法" | 全部 + 强制标注 `D 级 · 未验证` |
| 食材详情页 | 全部，按 level 排序并分组展示 |

API 参数：`evidence_policy = strict | balanced | all`，默认 `balanced`。

### 2.3 归一与 Crosswalk（对应 12.b）

**结论：值得做，但要简化 —— 不要一源一张表。**

Nutrigraph 维护了 27 份数据集所以按源拆表合理；我们初期只有 5-6 个数据源，一源一表是过度工程。做法：

```
data/crosswalk/
├── ingredient_canonical.csv   # canonical_id, canonical_name, category, aliases(;), nature
├── ingredient_source_map.csv  # canonical_id, source_name, source_id   ← 按源追加行，不建表
├── nutrient_map.csv           # canonical_id, china_food_code, usda_nutrient_no, unit
├── population.csv
└── symptom.csv                # 含口语别名：上火/燥热/口腔溃疡
```

`scripts/load_crosswalk.py` 统一装载进 PG 一张 `dict_alias` 表：

```sql
dict_alias(entity_type, canonical_id, alias, alias_norm, source, confidence, created_at)
-- UNIQUE(entity_type, alias_norm, canonical_id)
```

**实体链接四级漏斗**（从便宜到贵）：

1. 精确匹配 `alias_norm`（去空格/全半角/繁简统一）
2. PG `pg_trgm` 相似度 > 0.6
3. Milvus 向量 top-k（`kg_entity` 集合，阈值 0.82）
4. LLM 消歧（仅当 top-1 与 top-2 分差 < 0.05，且实体类型不同）

**别名回流**：第 2/3/4 级命中的口语词落 `dict_alias_candidate` 表，人工确认后进 `dict_alias` —— 系统越用越准，且不需要改代码。这是比"写 if-else 别名表"高明的地方，也是能讲的故事。

### 2.4 图谱构建管道与幂等写入

```
种子字典 → 建节点骨架 → LLM 抽三元组 → 候选表(待审) → 人工确认 → UNWIND MERGE 主图 → 向量化 → 聚合画像
```

**⚠️ 修正一处需求里的技术假设**

> needs.txt：「按「源节点 + 关系类型 + 目标节点」建唯一约束」

Neo4j 只支持**节点属性**唯一约束，**不支持关系唯一约束**。正确做法：

```cypher
-- 1) 节点唯一约束（这是幂等的根基）
CREATE CONSTRAINT ing_id IF NOT EXISTS FOR (n:Ingredient) REQUIRE n.canonical_id IS UNIQUE;
-- Nutrient / Population / Symptom / Taboo 同理

-- 2) 关系用确定性 MERGE + 业务键，理论幂等
UNWIND $rows AS row
MATCH (a:Ingredient {canonical_id: row.src})
MATCH (b:Ingredient {canonical_id: row.dst})
MERGE (a)-[r:INCOMPATIBLE_WITH {relation_key: row.relation_key}]->(b)
SET r += row.props
-- relation_key = sha1(src|TYPE|dst)，由应用层生成，保证重跑命中同一条边
```

3) **并发**：写入单线程（或 `apoc.periodic.iterate` parallelism=1），避免 MERGE 竞态产生重复边。
4) **批次可回滚**：每条边打 `batch_id`，出问题 `MATCH ()-[r]->() WHERE r.batch_id=$id DELETE r`。
5) 建索引加速：`(Ingredient.canonical_name)`、`(Symptom.name)`、`(dict_alias.alias_norm)`。

**候选表状态机**（PG）

```
kg_candidate(id, batch_id, src_type, src_id, rel, dst_type, dst_id,
             claim_text, evidence_type, evidence_level, sources jsonb,
             confidence, status[pending|approved|rejected|merged], reviewer, reviewed_at)
UNIQUE(batch_id, src_id, rel, dst_id)
```

审核入口：`python -m kg.review list --status pending --limit 50` / `approve --ids 1,2,3`，先 CLI 后接页面。

**抽取质量控制**：LLM 输出强制 JSON Schema + pydantic 校验 + 三元组必须引用原文片段（`source_locator` 精确到段落），无引用的候选直接丢弃 —— 这一条能砍掉大部分幻觉。

### 2.5 检索链路（LangGraph）

```
START → guard → normalize → link → route ─┬→ graph_query ────────┐
                                          ├→ vector_fallback ────┤→ assemble → generate → verify → memory_write → END
                                          └→ clarify ────────────┘
```

| 节点 | 职责 | 关键点 |
|---|---|---|
| `guard` | 安全前置 | 红旗症状/诊断类/剂量索取 → 固定话术，不进图谱 |
| `normalize` | 指代消解 + 繁简/全半角归一 | 用 PG 最近 K 轮补全「那豆腐呢」 |
| `link` | 实体链接 | 四级漏斗，返回 `[(canonical_id, type, score, method)]` |
| `route` | 意图 + 跳数选择 | 搭配/忌口/营养/替代/症状/通用 |
| `graph_query` | Cypher 模板 | 见下表，带 evidence 过滤 + 用户过敏/慢病排除 |
| `vector_fallback` | 降级 | 图 0 命中时查 `kb_chunks` |
| `assemble` | 上下文组装 | 结构化事实 + 证据标签；剂量做法只取 `summary` |
| `generate` | 严格接地 | 输出 JSON：`{answer, basis[], gaps[], confidence}` |
| `verify` | 自检 | answer 中每条断言能否在 `basis` 里找到出处；找不到则改写为"依据不足" |
| `memory_write` | 落库 | 消息 + 抽取到的用户画像增量 |

**Cypher 模板按意图**

| 意图 | 跳数 | 模板要点 |
|---|---|---|
| 搭配 | 1 | `(a:Ingredient{id})-[r:RECOMMEND_PAIR]-(b)` + `r.evidence_type IN $allowed` |
| 忌口 | 2 | `(a)-[r:CONTRA_POPULATION]->(p:Population)-[:HAS_SYMPTOM]->(s:Symptom)` |
| 药物/禁忌物 | 1 | `(a)-[r:CONTRA_ITEM]->(t:Taboo)` |
| 营养 | 1 | `(a)-[r:CONTAINS_NUTRIENT]->(n:Nutrient)` |
| 替代 | 1 + 排序 | `(a)-[r:SUBSTITUTE_FOR]-(b)` 再按 `CONTAINS_NUTRIENT` 重叠度重排 |

**过敏/慢病硬过滤**（记忆系统的真正用处）：

```cypher
WITH $allergens AS allergens
MATCH (a:Ingredient)-[r:RECOMMEND_PAIR]-(b:Ingredient)
WHERE NOT b.canonical_name IN allergens            // 硬排除，不靠 prompt
  AND r.evidence_type IN $allowed
RETURN a, r, b ORDER BY r.evidence_level, r.confidence DESC LIMIT $k
```

### 2.6 边界控制（三档回答）

| 档位 | 触发条件 | 输出形态 |
|---|---|---|
| **有据** | 至少 1 条 `A/B` 级关系 | 直接回答 + 引用列表 |
| **部分有据** | 只有 `C/D` 级 | 回答 + 显著标注「该说法为传统经验/民间流传，未经验证」 |
| **无据** | 图 0 命中且向量低分 | 固定话术「没有找到确切依据」+ 拒绝给具体食谱建议 |

**摘要/长文分离**：节点的 `detail`（做法步骤、剂量换算、适用条件）不进上下文，只在详情 API 返回。上下文里每个食材最多 40 字摘要，避免长尾噪声淹没图谱事实。

### 2.7 记忆（PostgreSQL）

```sql
session(id uuid pk, user_id, started_at, ended_at, summary)
message(id bigserial, session_id, role, content, meta jsonb, tokens int, created_at)
user_profile(user_id pk, allergies text[], conditions text[], diet_style, avoid_list text[], updated_at)
memory_fact(id bigserial, user_id, fact_type, content,
            embedding vector(1024), confidence, created_at)   -- pgvector
```

- **短期** = `message` 最近 10 轮 + `session.summary`（超长时滚动摘要）
- **长期** = `user_profile`（硬约束，进 Cypher 过滤）+ `memory_fact`（pgvector 语义召回，供个性化语气/偏好）
- **分工**：pgvector 只服务记忆（数据量小、需要事务一致）；Milvus 只服务图谱实体链接 + 文档块（数据量大、需要高性能）

### 2.8 Langfuse 怎么用（需求 11 · 头脑风暴）

**核心判断**：Langfuse 的价值不在"能看到 trace"，而在**把"回答可不可信"变成一个可监控的指标**。所以设计围绕这个目标。

**值得做的 6 件事**

1. **全链路 Trace 分层**
   一个提问 = 一个 trace；span 拆 `guard / normalize / link / graph_query / assemble / generate / verify`。
   用途：定位是"实体链接没命中"还是"生成跑偏"——这是 GraphRAG 最常见的两类故障，靠日志分不清，靠 span 一眼看出。

2. **把证据分布写进 trace metadata**
   metadata: `{hit_nodes, evidence_counts:{A:2,C:1}, policy:"balanced", graph_hit:true, degraded:false}`
   用途：直接跑 Langfuse 筛选 "degraded=true 的 query" 和 "A 级证据为 0 的 query" → 这两类就是最该优化的样本。**这是本项目最独特的一个用法。**

3. **Prompt 版本管理 + A/B**
   把 `generate` 的 grounding prompt 提到 Langfuse，版本化。
   实验：`严格接地版` vs `允许补充常识版` → 用同一 dataset 跑，对比 faithfulness。回答"到底要不要允许模型发挥"这个问题用数据说话。

4. **Dataset + Experiment 跑 Ragas**
   把评估集放 Langfuse dataset，每次改检索/prompt 跑一次 experiment，Langfuse 自动给出跨版本对比表。替代"跑个脚本看个分数"。

5. **Ragas 分数回写为 trace score**
   `faithfulness / relevancy / context_precision` 逐条写 score → 看趋势、设阈值告警。
   加分项：自定义 `abstention_correctness` 也回写。

6. **用户反馈闭环**
   前端 👍/👎 + 原因 → Langfuse score → 低分 trace 一键加入 dataset → 下一轮评估覆盖它。**数据回流闭环**，这是最有说服力的工程实践。

**成本与延迟**：DashScope token、Cypher 耗时、P95 延迟都挂在 trace 上，回答"图谱跳数增加多少延迟"这类问题。

**不建议做的（避免过度工程）**

- ❌ 把 Langfuse 当生产日志库存全量原文 —— 健康数据敏感，只存摘要 + hash，原文留 PG 并按用户可删。
- ❌ 自建 dashboard 重复 Langfuse 已有图表 —— 只在它没有的维度（如证据等级分布）做补充查询。
- ❌ 一期就上自托管集群 —— 先 Langfuse Cloud 或单机 docker-compose，等数据量说话。

**部署建议**：`docker compose` 起 langfuse + postgres + clickhouse（Langfuse v3 依赖 clickhouse）。若不想运维，先用 Cloud 免费额度，数据脱敏后再上。

### 2.9 Ragas 评估设计

| 指标 | 为什么需要 |
|---|---|
| `Faithfulness` | **第一指标**。答案是否完全由图事实支撑 —— 直接对应"不臆造" |
| `ResponseRelevancy` | 答案是否答到了问题 |
| `LLMContextPrecision` | 检索进来的事实有多少真用上了（图谱容易召回过多） |
| `NoiseSensitivity` | 弱证据（D 级）混进上下文时模型会不会被带偏 |
| **`AbstentionCorrectness`（自定义）** | 该拒答时是否拒答。通用 Ragas 没有这个指标，但对本项目是刚需 |

**评估集构造（60-100 条，四类均衡）**

| 类别 | 数量 | 期望行为 |
|---|---|---|
| 有据搭配 | 20 | 正常回答 + 引用 |
| 有据禁忌（人群/药物） | 20 | 严格回答 + A 级引用 |
| 民间说法 | 20 | 回答但标注"未验证" |
| 应弃答 | 20 | 明确说"没有确切依据"，**不给具体建议** |

**中文适配注意**：Ragas 默认 prompt 是英文，需 `prompt.adapt_language("chinese")` 适配后再跑，否则指标系统性偏低。LLM/Embedding 用 `LangchainLLMWrapper / LangchainEmbeddingsWrapper` 包 DashScope（参考 `gme_rag/Multimodal_RAG/evaluate/evaluate_self.py` 的封装方式）。

### 2.10 工程架构

```
nutri_graph/
├── app.py                    # FastAPI 入口（参考 msb_assistant/app.py）
├── config.py                 # 集中配置（沿用其风格）
├── api/
│   ├── chat.py               # /chat /chat/stream
│   ├── kg.py                 # /kg/ingredient/{id} /kg/graph/subgraph
│   └── health.py
├── graph/                    # LangGraph
│   ├── state.py  nodes.py  workflow.py  routers.py
├── kg/                       # 图谱域
│   ├── schema.py             # 节点/边定义、evidence 枚举
│   ├── constraints.py        # 建约束/索引
│   ├── normalize.py          # 归一 + 实体链接
│   ├── extract.py            # LLM 抽取 → 候选
│   ├── merge.py              # UNWIND 批量写入
│   ├── query.py              # Cypher 模板
│   └── review.py             # 审核 CLI
├── rag/                      # Milvus
│   ├── client.py  embed.py  entity_index.py  chunk_index.py  fallback.py
├── memory/
│   ├── short_term.py  long_term.py  profile.py
├── observability/
│   ├── langfuse_client.py  tracing.py  scores.py
├── eval/
│   ├── dataset.jsonl  metrics.py  run.py  adapt_prompts.py
├── data/
│   ├── crosswalk/   seeds/   docs/   candidates/
├── scripts/
│   ├── init_pg.py  init_neo4j.py  load_crosswalk.py  build_kg.py  ingest_docs.py
├── tests/
├── docker-compose.yml        # neo4j + milvus(+etcd+minio) + postgres + langfuse
├── requirements.txt
└── .env.example
```

**依赖基线**（对齐 `msb_assistant/requirements.txt` 已跑通的版本，避免重新踩坑）：

```
fastapi, uvicorn[standard]
pymilvus>=3.0, neo4j>=6.4
langgraph>=1.2, langchain>=1.4, langchain-core
dashscope>=2.0
psycopg[binary] / sqlalchemy, pgvector
langfuse>=3
ragas, datasets
pydantic>=2.14
```

---

## 3. Nutrigraph 三点借鉴 · 必要性裁决（需求 12）

| # | 借鉴点 | 裁决 | 理由 | 改造方式 | 成本 |
|---|---|---|---|---|---|
| a | 证据分层 measured/predicted + provenance 到 ingredient 级 | ✅ **必做** | 这正是本项目立意，成本极低，收益贯穿检索/输出/评估三处 | **不能照抄**：`measured/predicted` 是分子层面的"实测 vs 预测"，食疗场景应是 `guideline/measured/traditional/anecdotal` 四值 + `level A-D`；provenance 落为 `Ingredient.evidence_profile` 聚合 | 1 天 |
| b | canonical id + crosswalk | ✅ **做，但简化** | 长期收益大（数据源会持续增加），且能替代代码里的别名硬编码 | 不做一源一表；用**一张 `dict_alias` 通用表 + 一个 loader + 别名回流**。数据源映射用 CSV 追加行即可 | 1 天 |
| c | NeoVis.js 图浏览 | ⚠️ **部分做，换方案** | NeoVis 社区维护已明显放缓，对 Neo4j 5 / bolt 驱动的兼容有坑；且它做不了本项目的核心展示需求 —— **按证据等级着色** | MVP 先用 Neo4j Browser 验收；要做展示页时用 `neo4j-driver` 拉子图 JSON + **Cytoscape.js / vis-network** 自渲染，A 级绿 / C 级黄 / D 级灰，可视化"这条说法多可信" | 1 天（延后） |

**关于 c 的额外判断**：图浏览对本项目不是刚需，但**证据着色的图**是极强的演示素材（一屏就能说清"我们把民间说法和有据禁忌分开存了"）。所以：功能延后，但如果要做 demo/答辩，值得优先于其它锦上添花的功能。

---

## 4. 执行计划（6 阶段 / 约 11-12 人日）

### Phase 0 · 地基（0.5 天）
- `docker-compose.yml`：neo4j 5 / milvus(etcd+minio) / postgres 16(pgvector) / langfuse
- `config.py` + `.env.example`，`/health` 探活
- **验收**：`docker compose up` 后 4 个服务健康检查全绿

### Phase 1 · 图谱骨架与归一（1.5 天）
- 建约束/索引；5 类节点、7 类边的 schema 定义
- crosswalk CSV + `load_crosswalk.py` + `dict_alias` 表
- 种子数据：约 100 食材 / 30 营养素 / 15 人群 / 30 症状 / 10 禁忌对象
- **验收**：`西红柿` / `番茄` / `洋柿子` 三个词归一后指向同一个 `ING_00001`

### Phase 2 · 抽取与写入（2 天）
- `extract.py`：文档切片 → LLM 抽三元组（JSON Schema 约束 + 原文定位必填）
- `kg_candidate` 表 + `review.py` 审核 CLI
- `merge.py`：UNWIND 批量 MERGE + `relation_key` + `batch_id`
- **验收**：同一批数据连跑 3 次，节点数与边数**完全不变**；rejected 的候选不会进主图

### Phase 3 · 检索链路（2.5 天）
- Milvus 两集合：`kg_entity`（实体链接）、`kb_chunks`（降级检索）
- 四级实体链接漏斗 + 别名回流
- LangGraph 8 节点串联，意图路由 + 5 套 Cypher 模板
- **验收**：`上火了吃什么` 能映射到 `SYM_上火` 并给出图内答案；`石头能吃吗` 走弃答分支

### Phase 4 · 生成与边界（1 天）
- 严格接地 prompt + JSON 输出 + `verify` 自检
- 三档回答话术、红旗症状分支、免责声明
- **验收**：构造 10 个"图里没有"的问题，100% 不编造

### Phase 5 · 记忆（1 天）
- 4 张表 + pgvector；短期窗口 + 滚动摘要
- `user_profile` 作为 Cypher 硬过滤接入
- **验收**：说"我海鲜过敏"后，后续推荐里不再出现海鲜（且是数据库层面排除，不是 prompt 层面）

### Phase 6 · 可观测与评估（1.5 天）
- Langfuse：trace/span 埋点 + evidence metadata + score 回写
- Ragas：评估集 60 条 + 5 指标（含自定义弃答指标）+ 中文 prompt 适配
- **验收**：出一份基线报告（faithfulness / abstention_correctness 等），并能在 Langfuse 里按"低证据回答"筛出全部样本

### Phase 7 · 可选（1 天）
- 子图浏览页（证据着色） / 最小 Web UI / 食材详情页

---

## 5. 风险清单

### 5.1 🔴 最大风险：图谱是空壳（数据从哪来）

技术栈搭完只是容器，**没有高质量关系数据，项目就只有骨架**。建议的数据源分四层：

| 层 | 数据源 | 覆盖的关系 |
|---|---|---|
| 营养素 | 《中国食物成分表》第 6 版 | `CONTAINS_NUTRIENT`（B 级，实测） |
| 人群/指南 | 《中国居民膳食指南(2022)》、卫健委《既是食品又是中药材的物质目录》 | `CONTRA_POPULATION`、`RECOMMEND_PAIR`（A 级） |
| 药物相互作用 | 药品说明书、FDA 葡萄柚汁警告 | `CONTRA_ITEM`（A 级） |
| 民间说法 | 科普中国/丁香医生的食物相克辟谣、中医典籍 | `INCOMPATIBLE_WITH`（C/D 级） |

**缓解**：Phase 1 先手工录入 60-100 条**高质量种子边**（人工标注 evidence，可信度最高），再让 LLM 抽取去扩量。种子边同时就是评估集的 ground truth。

### 5.2 其他风险

| 风险 | 影响 | 缓解 |
|---|---|---|
| LLM 抽取幻觉 | 图谱被污染 | 原文定位必填 + 候选表人工确认 + 置信度阈值 |
| 实体链接召回不足 | 一问答不上来 | 别名回流机制；链接失败时明确澄清而非硬猜 |
| Neo4j 关系 MERGE 并发重复 | 数据脏 | 单线程写入 + `relation_key` + 批次回滚 |
| 医疗合规 | 法务风险 | 免责声明 + 红旗症状拦截 + 不输出剂量/诊断 |
| Ragas 中文指标偏低 | 评估失真 | 先 `adapt_language`，再用 20 条人工标注校准 |
| DashScope embedding 版本漂移 | 向量检索失效 | config 固定 `EMBEDDING_MODEL` + 集合名带模型版本后缀 |
| 范围膨胀 | 交付延期 | Phase 0-4 为 MVP；记忆/评估/图浏览按顺序增补 |

---

## 6. 待确认决策（会影响 Phase 0-1 的动手方式）

1. **种子数据**：你手上有食材/营养素字典吗（Excel/CSV）？还是要我先搭好 schema 并生成 100 条演示数据？
2. **「禁忌」节点建模**：接受 §2.1 的切分（Population=人 / Taboo=禁忌对象）吗？这会改变边的方向。
3. **部署形态**：本地 docker-compose 全栈（neo4j+milvus+pg+langfuse）够用，还是要考虑已有实例复用？
4. **Langfuse**：Cloud（快，但健康数据出境需评估）还是自托管（dokcer-compose，多 3 个容器）？
5. **MVP 边界**：先只跑通「搭配 + 忌口」两条链路，还是五类关系全铺？
6. **前端**：Phase 7 的图浏览要不要现在做（答辩演示价值高，但可延后）？
