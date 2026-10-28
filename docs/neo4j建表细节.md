# Neo4j 建表细节

> Neo4j 没有表。本文的「建表」指属性图的标签、关系类型、约束和索引，以及写入时实际落下去的属性。定义在 `kg/schema.py`，约束在 `kg/constraints.py`，在线查询在 `kg/query.py`，写入在 `kg/merge.py` 与 `scripts/load_seed.py`。

初始化：

```bash
python -m kg.constraints
python -m scripts.load_seed
```

连接：`NEO4J_URI`（默认 `bolt://localhost:7687`）、`NEO4J_USER`、`NEO4J_PASSWORD`。密码必须和 Neo4j 的 `NEO4J_AUTH` 一致，否则种子装载直接认证失败。

## 1. 标签一览

| 标签 | canonical_id 前缀 | 例子 | 在问句里的角色 |
|---|---|---|---|
| `Ingredient` | `ING_` | `ING_00001` | 食材。搭配、相克、营养、替代、人群禁忌的起点 |
| `Nutrient` | `NUT_` | `NUT_00006` | 营养素。只作为「含有」的终点 |
| `Population` | `POP_` | 孕妇、痛风患者 | `kind` 为 `group` 或 `disease` |
| `Symptom` | `SYM_` | 上火、失眠 | 症状问法的锚点 |
| `Taboo` | `TAB_` | 他汀类药物 | `taboo_type`：`drug` / `disease_state` / `cooking` / `extreme_pair` |

序号五位十进制。种子食材使用 `ING_00001` 到 `ING_00125` 这一段。补录使用序列 `ingredient_cid_seq`（PostgreSQL，起点 1000），格式化后为 `ING_01000` 起。序列不在 Neo4j 里。

## 2. 唯一约束

`kg/constraints.py` 幂等执行：

```cypher
CREATE CONSTRAINT IF NOT EXISTS
FOR (n:Ingredient) REQUIRE n.canonical_id IS UNIQUE;

CREATE CONSTRAINT IF NOT EXISTS
FOR (n:Nutrient) REQUIRE n.canonical_id IS UNIQUE;

CREATE CONSTRAINT IF NOT EXISTS
FOR (n:Population) REQUIRE n.canonical_id IS UNIQUE;

CREATE CONSTRAINT IF NOT EXISTS
FOR (n:Symptom) REQUIRE n.canonical_id IS UNIQUE;

CREATE CONSTRAINT IF NOT EXISTS
FOR (n:Taboo) REQUIRE n.canonical_id IS UNIQUE;
```

这是 MERGE 节点的锚。同一个 `canonical_id` 再写一次只会更新属性。

Neo4j 社区版不对关系做唯一约束。边的幂等靠应用层 `relation_key`（见第 5 节），不靠数据库约束。

## 3. 属性索引

同样由 `ensure_schema()` 创建，可重复执行：

```cypher
CREATE INDEX IF NOT EXISTS FOR (n:Ingredient) ON (n.canonical_name);
CREATE INDEX IF NOT EXISTS FOR (n:Ingredient) ON (n.category);
CREATE INDEX IF NOT EXISTS FOR (n:Nutrient) ON (n.name);
CREATE INDEX IF NOT EXISTS FOR (n:Symptom) ON (n.name);
CREATE INDEX IF NOT EXISTS FOR (n:Population) ON (n.name);
CREATE INDEX IF NOT EXISTS FOR (n:Taboo) ON (n.name);
```

在线问答的主路径用 `canonical_id` 点查（已有唯一约束，自带查找），这些索引服务于按名字或类别浏览、以及子图接口按名称取节点。没有全文索引，也没有向量索引；向量在 Milvus。

## 4. 节点属性

`NODE_PROPS` 是模型层列出的字段。真正写入时分两条路径，字段并不完全相同。

### 4.1 模型层

| 标签 | 属性 |
|---|---|
| Ingredient | `canonical_id`，`canonical_name`，`aliases`，`category`，`nature`，`summary`（进上下文的短摘要），`detail`（长文，只给详情），`evidence_profile` |
| Nutrient | `canonical_id`，`name`，`unit`，`category` |
| Population | `canonical_id`，`name`，`kind` |
| Symptom | `canonical_id`，`name`，`aliases`，`tcm_term` |
| Taboo | `canonical_id`，`name`，`taboo_type` |

`aliases` 在节点上是字符串数组，便于展示。检索用的别名主数据在 PostgreSQL `dict_alias`，不在图里做变长扫描。

### 4.2 种子装载实际写入

`scripts/load_seed.py`：

- Ingredient：`canonical_id`，`canonical_name`，`category`，`nature`，`aliases`，`summary`（仅当节点上还没有 summary 时，用标准名填上）。
- 其他标签：CSV 列经 `prop_map` 写进 `SET n += row.props`，键与 crosswalk 文件一致（名称、kind、taboo_type 等）。

种子节点没有 `batch_id`，也没有 `provisional`。

### 4.3 审核合并的白名单

`kg/merge.py` 的 `NODE_PROPS_WHITELIST` 决定补录能 `SET` 哪些键。标签不能参数化，所以用白名单而不是把外部 JSON 原样铺上去。

| 标签 | 允许写入 |
|---|---|
| Ingredient | `canonical_name`，`category`，`nature`，`aliases`，`summary`，`scientific_name`，`source_count`，`provisional`，`provenance` |
| Nutrient | `name`，`unit`，`category`，`provisional` |

`canonical_id` 在 MERGE 模式里，不放进 `props`。未知标签整组跳过。

Nutrient 故意没有 `provenance`。营养素是共享词表，回滚某一批补录时若按 `provenance` 删除，会把其他食材还在用的节点一起删掉。回滚后可能留下孤立营养素节点。

`provenance` 是字符串，内容包含 `batch_id`（如 `bundle:3`）。回滚用 `n.provenance CONTAINS $bid`。

已存在节点上的 `summary`、`provenance`、`canonical_name`、`category` 在 `kg/promote.py` 里是粘性属性：旧值非空时不被新 bundle 覆盖。避免一行「北豆腐」把已经承载豆腐数据的节点改名。

## 5. 关系类型

| 类型 | 模式 | 语义 |
|---|---|---|
| `CONTAINS_NUTRIENT` | `(Ingredient)-[]->(Nutrient)` | 每 100g 口径的含量 |
| `RECOMMEND_PAIR` | `(Ingredient)-[]-(Ingredient)` | 推荐搭配。查询无方向 |
| `INCOMPATIBLE_WITH` | `(Ingredient)-[]-(Ingredient)` | 不宜同食，民间说法放这里 |
| `CONTRA_POPULATION` | `(Ingredient)-[]->(Population)` | 某人群慎食或忌食 |
| `SUBSTITUTE_FOR` | `(Ingredient)-[]-(Ingredient)` | 替代。查询无方向 |
| `CONTRA_ITEM` | `(Ingredient)-[]->(Taboo)` | 药物、疾病状态、烹调极端组合 |
| `HAS_SYMPTOM` | `(Population)-[]->(Symptom)` | 人群关联的症状，供症状问法中转 |

合并时七种关系都写成有方向的 `MERGE (a)-[r:TYPE {relation_key}]->(b)`。种子和合并使用同一 `REL_LABEL` 映射。查询模板对食材—食材使用无方向模式 `-[]-`，所以方向不影响「能不能查到」。

### 5.1 公共属性

模型层 `REL_COMMON_PROPS`：

| 属性 | 类型约定 | 说明 |
|---|---|---|
| `evidence_type` | string | `guideline` / `measured` / `traditional` / `anecdotal` |
| `evidence_level` | string | `A` / `B` / `C` / `D` |
| `sources` | **string** | JSON 数组的文本，不是 map、也不是 map 列表 |
| `claim_text` | string | 原始断言 |
| `confidence` | float | 排序 |
| `batch_id` | string | 补录批次，回滚键。种子边可以没有 |
| `review_status` | string | 合并写入 `merged` |
| `created_by` | string | 模型层预留 |
| `relation_key` | string | 见下 |

合并路径还会写：`source_locator`（如 `fdc:<id>#<nutrientNumber>`）、`source_count`（整数）、`provisional`（布尔）。

### 5.2 各关系额外属性

| 关系 | 额外属性 | 合并路径是否写入 |
|---|---|---|
| `CONTAINS_NUTRIENT` | `amount`，`unit`，`per` | 含量主要进 `claim_text`；`unit` 可能在种子边。子图展示会从断言回捞数字 |
| `RECOMMEND_PAIR` | `effect`，`rationale` | 种子 CSV 若有则在装载时写入；补录翻译当前把说明放进 `claim_text` |
| `INCOMPATIBLE_WITH` | `reason`，`severity` | 同上 |
| `CONTRA_POPULATION` | `condition` | 同上 |
| `SUBSTITUTE_FOR` | `basis`，`similarity` | 同上 |
| `CONTRA_ITEM` | `interaction` | 同上 |
| `HAS_SYMPTOM` | `relation` | 同上 |

查询模板返回时用 `effect` / `reason` / `basis` 等别名，缺了就靠 `claim_text`。

### 5.3 `sources` 为什么是字符串

Neo4j 属性只接受原始类型及其数组。下面这种写法会抛 `CypherTypeError`：

```cypher
SET r.sources = [{type:'document', title:'…'}]
```

`kg.schema.sources_to_json` 使用 `json.dumps(..., ensure_ascii=False)`。读出来的代码需要 `json.loads`。PostgreSQL `kg_candidate.sources` 保持 JSONB，两边不要混用同一种驱动绑定。

一条来源对象的常见键：`type`，`title`，`publisher`，`year`，`url`，`locator`。种子边写入 `type/title/publisher/year`。

### 5.4 `relation_key`

```python
sha1(f"{src_canonical_id}|{rel}|{dst_canonical_id}".encode("utf-8")).hexdigest()
```

实现在 `kg.schema.relation_key`。这是去重键，不是安全签名（用的是 SHA-1）。

MERGE 子句：

```cypher
MATCH (a {canonical_id: $src_id})
MATCH (b {canonical_id: $dst_id})
MERGE (a)-[r:REL_TYPE {relation_key: $relation_key}]->(b)
SET r.evidence_type = $evidence_type,
    r.evidence_level = $evidence_level,
    r.claim_text = $claim_text,
    r.sources = $sources,
    r.confidence = $confidence,
    r.source_locator = $source_locator,
    r.source_count = $source_count,
    r.provisional = $provisional,
    r.batch_id = $batch_id,
    r.review_status = 'merged'
```

关系类型不能参数化，调用前用 `REL_LABEL` 白名单卡住。两端节点必须已经存在：`merge_nodes` 在 `merge` 之前跑。目标 id 为空或等于 `UNRESOLVED` 的候选边直接跳过。

同一 `(src, rel, dst)` 再合并会命中同一条边并覆盖证据字段。它不会因为 claim 文本不同就长出第二条边。若业务上需要「同一对食材、两种证据并存」，当前键表达不了，要改键的组成。

## 6. 在线查询模板

全部在 `kg/query.py` 的 `TEMPLATES`。参数：

| 参数 | 含义 |
|---|---|
| `$cid` | 锚点 `canonical_id` |
| `$allowed` | 该意图允许的 `evidence_type` 列表 |
| `$k` | `GRAPH_MAX_RESULTS`，默认 8 |
| `$allergens` | 画像过敏名 + 忌口名 |
| `$include_provisional` | 默认跟随配置 `INCLUDE_PROVISIONAL` |

每条都包含：

```cypher
AND ($include_provisional OR coalesce(r.provisional, false) = false)
```

`coalesce` 让种子边上没有该属性时视为非 provisional，严格模式不会误删演示数据。

| 意图 | 匹配骨架 | 排序 |
|---|---|---|
| `pair` | `(Ingredient)-[RECOMMEND_PAIR]-(Ingredient)`，排除过敏名 | `evidence_level`，再 `confidence DESC` |
| `incompatible` | `(Ingredient)-[INCOMPATIBLE_WITH]-(Ingredient)`，排除过敏名 | 同上 |
| `contra_population` | `(Ingredient)-[CONTRA_POPULATION]->(Population)`，可选 `(Population)-[:HAS_SYMPTOM]->(Symptom)` | `evidence_level` |
| `contra_item` | `(Ingredient)-[CONTRA_ITEM]->(Taboo)` | `evidence_level` |
| `nutrition` | `(Ingredient)-[CONTAINS_NUTRIENT]->(Nutrient)` | `evidence_level` |
| `substitute` | `(Ingredient)-[SUBSTITUTE_FOR]-(Ingredient)`，排除过敏名 | `evidence_level` |
| `symptom_remedy` | `(Symptom)<-[:HAS_SYMPTOM]-(Population)<-[CONTRA_POPULATION]-(Ingredient)` | `evidence_level` |

`evidence_level` 是字符串，`ORDER BY r.evidence_level` 的字母序正好是 A、B、C、D。

症状模板回答的是「这类人该避开什么」，没有「推荐吃什么」的边。

## 7. 探索用 Cypher

`kg/subgraph.py` 另有三组只读查询，不参与生成：

- 邻域：从给定 `canonical_id` 出发，关系类型 `$rels` 限制在七种之内，遵守 provisional 开关，`LIMIT` 为 `SUBGRAPH_MAX_EDGES`（60）。`depth=2` 只对一跳里的食材再扩，最多 `SUBGRAPH_DEPTH2_SEEDS`（4）个种子。
- 直接关系：两个 id 之间七种关系里存在的边。
- 最短路径：`shortestPath`，跳数取 `min(SUBGRAPH_MAX_HOPS, 4)`，默认 3。路径上的边打上 `path` 标记。

返回前裁剪到 `SUBGRAPH_MAX_NODES`（40）。裁剪是应用层的，不是图算法的提前终止保证；深度 2 的循环会在节点数到达上限时停止继续扩展。

## 8. 回滚

```cypher
MATCH ()-[r]->() WHERE r.batch_id = $bid
DELETE r
RETURN count(r) AS n;

MATCH (n) WHERE n.provenance CONTAINS $bid
DETACH DELETE n
RETURN count(n) AS c;
```

只应删除「这一批新建」的节点。挂到种子 `canonical_id` 上的别名补录没有新的 `provenance`，节点保留，只删该 `batch_id` 的边。

执行回滚的 Python 还会把 PostgreSQL 里该批 `merged` 候选改回 `pending`，方便修好之后再合并。图和候选表是两个事务：图先删、库后改。中途失败时以日志为准再跑一次；MERGE 与 DELETE 都按键幂等。

## 9. 演示规模与重复装载

README 记录的种子是 125 个节点 + 40 条边（crosswalk 别名约 360 条，别名在 PostgreSQL）。`load_seed` 用 `UNWIND` + `MERGE`，重复执行不增加节点数，关系按 `relation_key` 覆盖属性。

`load_seed --edges-only` 在节点已经存在时只装边。

## 10. 和 PostgreSQL 候选表的对应

图是发布态。下面这些行还在 Postgres 时，Neo4j 里不应该出现：

| Postgres | 图上对应物 |
|---|---|
| `kg_node_candidate.status=approved` | 尚未 MERGE 的节点草稿 |
| `kg_candidate.status=approved` | 尚未 MERGE 的边草稿 |
| `status=merged` | 图中已有同 `canonical_id` / `relation_key` 的元素 |
| `kg_food_bundle` | 不直接对应元素；approve 之后才变成上面两张候选 |

因此排查「审核通过了但问不到」时先看候选是 `approved` 还是 `merged`，再看边的 `evidence_type` 是否被意图策略滤掉，最后看 `provisional` 与 `INCLUDE_PROVISIONAL`。
