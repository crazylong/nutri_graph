# 后台食物查询、删除重录与食材切分（v6.1）

> 来源：[docs/needs/needs_v6.md](../needs/needs_v6.md)
> 评审：[docs/review/review_v6.md](../review/review_v6.md)
> 状态：**v6.1 / 已按评审修订（2026-10-29）** —— 施工以本文为准。仍不含代码。
>
> 一句话结论：四条需求都做。删除按食材 id，并清掉别名、向量和进程内缓存，每个曾用名留墓碑。重录不先删节点、不走 USDA、不改 `provenance`；只覆盖名称/分类/摘要，并替换该食材的营养边。普通导入不能改图上已有食材的名称。

v6.0 里有四处会让功能看起来做了、其实没生效，本版改掉：

1. 粘性属性只守在 `kg_node_candidate`。种子没有候选行，`merge_nodes` 的 `SET n += row.props` 会直接改图上的名称。
2. 删 `dict_alias` 之后，Milvus `kg_entity` 和 `all_aliases()` 缓存仍会把旧词链回去。
3. `merge()` 是 `MERGE ... SET`，删掉的营养素边不会消失。
4. 重录若改写 `provenance`，之后 `rollback()` 会把食材节点连同搭配、禁忌一起删掉。

评审里这三件不收进本版：候选列表补分页；关系图限制只能两个食材；把历史 `amount` 说成字符串。`kg_candidate.amount` 是 `DOUBLE PRECISION`，排序风险来自老边没有 `amount`、数字只在 `claim_text` 里。

---

## 零、结论摘要

| 项 | 判定 | 施工口径 |
|---|---|---|
| 前台去掉食物导入入口 | 做 | 只删首页顶栏链接。`/import.html` 与 `/kg/import/*` 保留 |
| 按食物名查、认别名 | 做 | 复用 `AliasStore`，详情用 `neighborhood`。不调 `POST /kg/compare` |
| 按营养成分查 | 做 | 先定到一个营养素，`toFloat(r.amount)` 降序，每页 15，另给总数。无数值不进主序 |
| 多食材关系图 | 做 | 后台重写等价 SVG，2～4 个食材。模糊命中先确认。未收录不登记 |
| 删除 | 做 | 按 `canonical_id` 删节点和它自己的边。营养素节点不动。不用 `rollback()` |
| 重录 | 做 | 不先删节点。保留搭配/禁忌和原 `provenance`。替换营养边。名称/分类/摘要可覆盖 |
| LLM 拆食材 | 做 | 只拆没切开且链接失败的残余。这次拆出的词最多登记 3 个。失败保持整段 |
| 候选关系按钮 | 做 | `merged` / `rejected` 不显示操作；`approved` 只留驳回 |

**不做**：不把 Neo4j 当成日常入库口（删除是显式下架，重录仍走审核再 `kg.merge`）；不引入 echarts；不改候选列表的 `limit=50`；不在开发机起库或跑 pytest。

---

## 一、需求对照

原文见 [docs/needs/needs_v6.md](../needs/needs_v6.md)。

1. 前台首页去掉食物导入入口。
2. 后台增加已有食物查询：按名认别名、按营养成分分页、多食材出图、列表和详情都能删除和重录。
3. 「猪脚天麻当归煲汤合适吗」要用 LLM 拆出其中的食材，不能整段登记。
4. 候选关系按状态决定右侧操作。`merged` 不再显示通过/驳回。

---

## 二、删除和重录

查询对象包含种子食材（胡萝卜、鸡蛋、番茄）。种子没有 bundle，不能打回待审核。USDA 收不到天麻、当归、猪脚，重录走管理员手工录入和现有审核。

### 2.1 写图时的粘性

[kg/promote.py](../../kg/promote.py) 第 50–55 行 `_STICKY_PROPS` 含 `summary`、`provenance`、`canonical_name`、`category`。第 219–238 行只在 `kg_node_candidate` 已有行时合并旧值；没有行就整份 INSERT。种子由 `scripts/load_seed.py` 直写图，没有候选行。随后 [kg/merge.py](../../kg/merge.py) 第 175–180 行 `SET n += row.props` 无条件覆盖图属性。

因此「不加重录标记就改不掉名称」只对**已经有候选行**的食材成立。对种子不成立。

施工：

- 候选表这一层的粘性规则保留。
- `merge_nodes` 写图前看图上是否已有该 `canonical_id`。
  - 没有节点：白名单属性照常写入，包括名称。
  - 已有节点且不是重录：从本次 `SET` 里拿掉 `_STICKY_PROPS`。普通导入不能改已有食材的名称、分类、摘要、`provenance`。这是行为变更：今天第一次导入种子名会意外改掉图上的名称，改完之后不能。
  - 已有节点且是重录：允许覆盖 `canonical_name`、`category`、`summary`。**`provenance` 永远不改**，避免 `rollback()`（[kg/merge.py](../../kg/merge.py) 第 334 行起）把这个节点当成「本批新建」删掉。

重录标记落在 `kg_food_bundle.re_record`，批准时抄到 `kg_node_candidate.re_record`。`merge_nodes` 读候选行上的标记。没有标记的旧行一律当普通导入。

### 2.2 营养边先删后写

[kg/merge.py](../../kg/merge.py) 第 286–300 行按 `relation_key` `MERGE` 再 `SET`。同一条边会被更新，食材上多出来的旧营养边不会消失。重录若从表单拿掉维生素 C，图上那条边还在。

重录合并时，在同一个 Neo4j session 里：

1. `MATCH (n:Ingredient {canonical_id: $cid})-[r:CONTAINS_NUTRIENT]->() DELETE r`
2. 再写入本批新的营养边。

只删 `CONTAINS_NUTRIENT`。`RECOMMEND_PAIR`、`INCOMPATIBLE_WITH`、`CONTRA_*`、`SUBSTITUTE_FOR` 保留。两步失败则整次 session 失败，不能留下一个营养边被清空、新边没写上的食材。

排序和展示只认边上的数值：`toFloat(r.amount)`。`amount` 为 null 的不进「从高到低」主序，接口里单独计数，不在 Cypher 里解析 `claim_text`。单位用营养素节点的 `n.unit`，边上的 `r.unit` 只在节点单位为空时顶上。这与 [kg/query.py](../../kg/query.py) 第 56–63 行「`n.unit` 展示、`r.amount` 取数」一致。

### 2.3 删除

`MATCH (n:Ingredient {canonical_id: $cid}) DETACH DELETE n`。只删这个食材节点和它自己的边。共享 `Nutrient` 节点不动。不用 `rollback()`，不用 `provenance CONTAINS`。

同一请求里还要做完这四件，否则删完仍能被问出来或被下一轮合并写回去：

1. 删 `dict_alias` 里 `entity_type='Ingredient'` 且 `canonical_id` 等于它的全部行。随后 `get_alias_store().all_aliases(refresh=True)`。`exact_lookup` 直接查库，最长匹配走的是这份进程内缓存（[kg/normalize.py](../../kg/normalize.py) 第 125–131 行）。
2. 按 `canonical_id` 删 Milvus `kg_entity` 里的向量。在 [rag/client.py](../../rag/client.py) 加按 id 删除，不跑 `scripts/index_entities.py` 的全量重建。Milvus 不可用时，图和别名的删除仍然算成功，审计里记 warning 并在接口里返回，不回滚已删的图。
3. 墓碑。每个曾用名（规范名和每一个别名）各写一行 `kg_ingredient_request`：`food_key` 用归一后的词，`status='rejected'`，`reject_reason='deleted_by_admin'`。已有行则把状态改成 `rejected`，避免还停在 `queued` 上被抓取。`register()` 第 342–349 行已经对 `rejected` 直接返回、不再建抓取任务，不要另做一套状态机。只给规范名写墓碑不够：「西红柿」删了，下次问「番茄」仍会重新登记。
4. 已 `merged` 的 bundle / `kg_node_candidate` / `kg_candidate` 保持 `merged`，不打回 `pending`。打回去会把旧数据再次写入图。

种子食材允许删。确认框写出将丢掉的边数。批量单次最多 20 条。

### 2.4 重录

不先 `DETACH DELETE`。用当前名称、别名、营养值预填录入表。提交后进 `kg_food_bundle`，`re_record=true`，走现有审核。

后台没有单条手工录入。不要复用公开的 `POST /kg/import/manual`，那条按 IP 限流。重录走管理员接口，校验仍用 `kg/importer.py` 的 `validate_row`。

批准后的合并：

- 覆盖该节点的 `canonical_name`、`category`、`summary`。
- 不改 `provenance`。
- 按 §2.2 替换营养边，搭配和禁忌保留。
- [kg/promote.py](../../kg/promote.py) 第 259–267 行会把同一 `canonical_id` 的候选行改回 `approved`。重录能再次写图靠的是这条更新。不要改成 `DO NOTHING`。种子第一次重录没有旧候选行，走第 225–238 行的 INSERT，同样写成 `approved`。

### 2.5 审计

`kg_request_action` 只挂在未收录请求上，盖不住「删了一个食材」。迁移 `024` 新增 `kg_food_admin_action`：`actor`、`action`（`delete` / `rerecord`）、`canonical_id`、`food_name`、`edge_count`、`detail`（jsonb，含别名、warning）、`created_at`。删除和提交重录各写一行。

---

## 三、查询

### 3.1 一个搜索框，三种结果

后台新页面「食物查询」。菜单加在 [admin-web/src/views/Layout.vue](../../admin-web/src/views/Layout.vue)，路由加在 [admin-web/src/router.js](../../admin-web/src/router.js)。查询复用 `AliasStore` / `EntityLinker`，不另写一套匹配，避免后台查得到、问答查不到。

分流：

- 整句精确命中营养素别名 → 含量列表。
- 整句精确命中食材别名或规范名 → 食材列表，点进详情。
- 两边都精确命中时不猜。胡萝卜的别名里就有「胡萝卜素」，而胡萝卜素也是营养素名（[data/crosswalk/ingredient_canonical.csv](../../data/crosswalk/ingredient_canonical.csv) 第 14 行）。接口返回两种命中，页面让人选「当作食材」或「当作营养素」。
- 用空格、和、跟、与、顿号切开，且至少两个食材名 → 关系图。
- 其余走字级相似，结果里标明精确 / 别名 / 模糊。模糊命中要确认后才进详情或画进图。

详情用 [kg/subgraph.py](../../kg/subgraph.py) 的 `neighborhood`，展示别名、营养含量和一跳关系。

不要调用 [api/kg.py](../../api/kg.py) 第 184 行 `POST /kg/compare`。第 206–208 行会把未收录词 `register` 进用户队列。未收录的名字只在页面上提示。

### 3.2 营养成分

[data/crosswalk/nutrient_map.csv](../../data/crosswalk/nutrient_map.csv) 第 2 行维生素 C 的 `aliases` 为空。补上「维C;VC」，用现有 [scripts/sync_nutrients.py](../../scripts/sync_nutrients.py) 写入图节点和 `dict_alias`。同步后对运行中的进程做 `all_aliases(refresh=True)`。`kg/nutrients.py` 按「中文名,单位」识别，不用改。

含量查询在 Neo4j：先解析到一个营养素，再 `ORDER BY toFloat(r.amount) DESC`。`SKIP/LIMIT` 与 `count` 分两次查，每页 15。没有 `amount` 的不进这一页，响应里给出无数值的条数。能对上多种营养素时返回候选，不把毫克和克排在一列。

分页用 Element Plus 的 `el-pagination`。后台现有列表没有分页控件，营养列表和食材搜索结果自己加。候选关系列表本版不加。

### 3.3 关系图

[kg/subgraph.py](../../kg/subgraph.py) 第 487 行 `compare_many` 已能把 2～4 个食材收成 `{nodes, edges}`。前台 [frontend/assets/app.js](../../frontend/assets/app.js) 第 622 行是手写 SVG，后台是 Vue，没有共享构建。不要去抽一个不存在的公共模块。在后台新写一个 SVG 组件，节点、边和配色与前台一致。

上限保持 2～4，与现有对照相同。需求举例是两个，不因此把接口收成只能两个。`sg.resolve` 的字级模糊可能猜错，图上标出命中来源；模糊的要确认后才画。

### 3.4 候选关系按钮

[admin-web/src/views/Candidates.vue](../../admin-web/src/views/Candidates.vue) 操作列现在不看行状态。筛选是单选（第 5–8 行加 `?status=`），按当前筛选决定按钮即可。

[api/admin.py](../../api/admin.py) 第 201–203 行只更新 `pending` 和 `approved`。

| 状态 | 通过 | 驳回 |
|---|---|---|
| pending | 显示 | 显示 |
| approved | 不显示 | 显示 |
| merged | 不显示 | 不显示 |
| rejected | 不显示 | 不显示 |

`sensitive_block > 0` 时通过按钮保持禁用。

### 3.5 前台入口

[frontend/index.html](../../frontend/index.html) 第 70 行删掉「食物导入」链接。页面和公开接口保留。

---

## 四、LLM 切分

[graph/nodes.py](../../graph/nodes.py) 第 99–118 行先做词典最长匹配，残余交给 [kg/request.py](../../kg/request.py) 的 `extract_candidates`。停顿词能去掉「煲汤」「合适」，「猪脚天麻当归」中间没有分隔，6 个字又通过了 2–8 字粗筛，整段被当成一种食材。[tests/test_web_answer.py](../../tests/test_web_answer.py) 第 118 行把这个结果断言死了，实现时要改掉。

[graph/nodes.py](../../graph/nodes.py) 第 162–169 行 `_eligible_terms` 全局只登记前 2 个，用来防刷。样例有猪脚、天麻、当归三个。不要把全局上限改成 3。只给「同一次 LLM 从一个残余片段拆出来的词」单独放行，最多 3 个。其他未识别词仍最多登记 2 个。

规则：

- 触发：残余片段长度 ≥ 4，且 `linker.link` 没有命中。
- 模型返回的每个词必须是该片段的子串，否则丢掉。
- 拆开成功就不要再把整段登记成一种食物。
- 超时 3 秒。超时、模型不可用、或一个合法子串都没有，保持现在的整段行为。异常不能让 `/chat` 500。
- 拆词结果写日志：原片段、拆出的词、是否超时。不为此新建表。
- [kg/normalize.py](../../kg/normalize.py) 第 236 行 `EntityLinker` 已有 `llm` 参数但没有使用。切分从这里注入，测试用假实现覆盖「拆开」和「失败保持原状」，不打真实模型。

---

## 五、接口

全部挂在现有 [api/admin.py](../../api/admin.py) 第 34 行的 `/admin` 路由上，自带 `require_admin`。

| 方法 | 路径 | 作用 |
|---|---|---|
| GET | `/admin/kg/foods?q=` | 食材搜索。响应含 `matches[]`（`canonical_id`、名称、命中方式）和可选的 `nutrient_matches[]`，两边都有时由前端选择 |
| GET | `/admin/kg/foods/{cid}` | 详情：别名、`neighborhood` 子图 |
| GET | `/admin/kg/nutrients?q=&page=` | 先解析营养素。未唯一时返回 `candidates[]`。唯一时返回 `items[]`、`total`、`missing_amount`，`page` 从 1 起，每页 15 |
| POST | `/admin/kg/foods/relations` | 体：`foods` 2～4 个。返回 `compare_many` 的子图、每个名字的命中方式、未收录名单。不写 `kg_ingredient_request` |
| POST | `/admin/kg/foods/delete` | 体：`canonical_ids`，1～20 个。返回每个 id 的边数、墓碑词、Milvus warning |
| POST | `/admin/kg/foods/{cid}/rerecord` | 体与 `validate_row` 的一行相同，预填后的修改。创建 `re_record=true` 的 bundle，进入现有审核 |

错误沿用现有 `detail.code` / `detail.message`。图不可用 503，id 不存在 404，超过 20 条 400。

---

## 六、迁移 024

当前最新是 [migrations/023_request_triage.sql](../../migrations/023_request_triage.sql)。新增 `migrations/024_food_admin.sql`：

- `kg_food_bundle.re_record BOOLEAN NOT NULL DEFAULT false`
- `kg_node_candidate.re_record BOOLEAN NOT NULL DEFAULT false`
- 表 `kg_food_admin_action`，字段见 §2.5

墓碑复用 `kg_ingredient_request.status='rejected'` 和已有的 `reject_reason`，不新加状态值。

---

## 七、施工顺序

1. 首页去掉食物导入链接。候选关系按 §3.4 藏按钮。
2. 迁移 024。后台食物查询：食材搜索、详情、歧义选择。
3. 维生素 C 简称、`sync_nutrients`、含量分页。
4. 关系图组件。模糊命中先确认，未收录不登记。
5. 删除（图、别名、缓存、向量、墓碑、审计）和重录（预填、`re_record`、写图粘性、营养边替换、`provenance` 不动）。
6. LLM 切分、放行最多 3 个拆出词、改第 118 行断言。

验证在服务器 `8.134.253.167` 上做。不在开发机起库或跑 pytest。图相关断言需要 `ALLOW_GRAPH_TESTS=1`，测试库与生产图是同一个，只跑下面点名的用例。

---

## 八、测试与验收

必须有的断言：

- `extract_candidates("猪脚天麻当归煲汤合适吗")` 的旧断言改成对 LLM 切分的断言：假模型返回猪脚、天麻、当归三个子串时，三个都进入登记；返回「天」「麻」这种非子串时丢掉；抛错或超时则仍是整段「猪脚天麻当归」，且接口不 500。
- 普通导入已有种子名，图上 `canonical_name` 不变。带 `re_record` 时名称可改，`provenance` 不变，旧的搭配边还在，营养边与新表一致，从表单删掉的营养素不再有边。
- 删除后 `dict_alias` 无该 id，`all_aliases(refresh=True)` 后最长匹配不再命中，每个曾用名的 `register()` 返回已驳回而不新建抓取任务。
- 维C 解析到维生素 C；有数值的按降序；每页 15；响应含 `total`；`amount` 为空的不在 `items` 里。
- 候选列表在 `merged` 下不渲染通过和驳回。

服务器上再看一遍：首页无「食物导入」、西红柿进入番茄所在的那条食材（规范名是西红柿，别名含番茄）、胡萝卜和鸡蛋出图、`merged` 无按钮、样例问句拆出猪脚 / 天麻 / 当归。

---

## 九、已拍板

1. 种子食材允许删除。确认框展示边数，单次最多 20。
2. 重录保留搭配和禁忌，替换营养边，可改名称、分类、摘要，不改 `provenance`。
3. 普通导入不能改图上已有食材的名称、分类、摘要、`provenance`。
4. 公开 `/import.html` 和 `/kg/import/*` 保留，只去掉首页入口。
5. 关系图仍是 2～4 个食材。模糊命中先确认。
6. 候选关系列表本版不加分页。
