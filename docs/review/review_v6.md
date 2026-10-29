# plan_v6 评审（对照 needs_v6）

> 对照 `docs/needs/needs_v6.md` 与 `docs/plan/plan_v6.md`，逐条核对了仓库现状（`文件:行号` 已复验）。
> 未改代码。日期：2026-10-29。

## 1. 结论

四条需求都该做，方向正确。计划里的事实核对质量高 —— 抽查 15 处引用基本准确（`api/kg.py:184` 的 `/compare`、`api/kg.py:207` 的 `register`、`api/admin.py:201-203` 的 `status IN ('pending','approved')`、`kg/subgraph.py:376/487`、`graph/nodes.py:99-118`、`kg/request.py:51-63/74/145`、`frontend/index.html:70`、`Layout.vue:11-12` 都对得上，只有 `Candidates.vue:25-29` 这类 off-by-one）。

但不能直接开工。有四处会让「功能看起来做了、其实没生效」，必须先改：

1. §二「带重录标记才覆盖粘性属性」在图谱写入层**没有开关**，在种子食材上这个标记是空的。
2. 删除只清了 `dict_alias`，还留着 Milvus 实体向量、进程内别名缓存两处活引用。
3. §二「替换 `CONTAINS_NUTRIENT`」按现有 `merge()` 做不到 —— 它是合并不是替换，被删掉的营养素会留图里。
4. 重录会改写 `provenance`，之后 `rollback` 会把整个食材节点连搭配、禁忌一起删掉。

另外作为施工文档还缺四块：接口契约、迁移编号、测试清单、删除审计。见 §5。

## 2. 逐条判定

| 需求 | 计划做法 | 判定 | 备注 |
|---|---|---|---|
| 1 前台去入口 | 删 `index.html:70`，保留页面与接口 | ✅ | `index.html` 是 `no-store`，不用 bump `?v=` |
| 2.1 按名查、认别名 | 复用 `AliasStore` + `neighborhood` | ✅ | 种子规范名是「西红柿」、别名「番茄」（`ingredient_canonical.csv:2`），详情标题会显示西红柿，与需求举例方向一致 |
| 2.2 按营养查、15/页、降序 | 先定一个营养素再 `ORDER BY amount DESC` | ⚠️ 口径没写完 | 见 §4.3 |
| 2.3 多食材出图 | `compare_many`，不调 `/kg/compare` | ✅ | 后半句是关键判断，保留 |
| 2.4 删除/重录 | 按 `canonical_id` 删；重录不先删节点 | ⚠️ 有洞 | 见 §3 |
| 3 LLM 拆食材 | 残余 ≥4 且链接失败才调 | ✅ | 少算了四件事，见 §4.2 |
| 4 候选按钮按状态 | pending/approved 显示、merged/rejected 隐 | ✅ | 接口行为核对无误 |

§3.4 有一处措辞会误导实施，见 §4.4。

## 3. 四处阻塞

### 3.1 粘性属性只存在于候选表，图谱层没有

`kg/promote.py:219-223` 读的「旧值」是 `kg_node_candidate` 的 props，**不是图谱节点**；`kg/promote.py:225-238` 在没有候选行时把整份 props 直接 INSERT。

种子 50 个（`data/crosswalk/ingredient_canonical.csv`）由 `scripts/load_seed.py` 直写图，从来没有候选行。而 `_resolve_canonical_id`（`kg/promote.py:158-170`）又能把种子名解析到 `ING_000xx`，于是**普通导入**会给种子建一条全新候选，`kg/merge.py:175-180` 的 `SET n += row.props` 无条件覆盖 `canonical_name` / `category` / `summary`。

后果有两条：

- §二 那句「不加重录标记，再次导入改不掉错误名称和分类」对种子不成立。而要重录的恰恰是胡萝卜、鸡蛋、番茄这类种子。
- §四 第 5 步「仅重录绕过粘性字段」在种子上等于没绕过 —— 没有保护可绕。

修法：粘性过滤下沉到 `merge_nodes` 写图之前，按候选行的 `re_record` 标志决定是否把 `_STICKY_PROPS` 从 payload 里剔掉；候选表那层保留不动。这会把「普通导入能否改种子名」从「能（意外）」变成「不能」，属于行为变更，需要在 §五 加一个拍板点。我的建议是不能改。

### 3.2 删除之后还留着两处活引用

- **Milvus `kg_entity`**：`scripts/index_entities.py` 只有全量重建（先清空再写），`rag/client.py` 没有按 id 删除的接口。删完节点，向量兜底（`graph/nodes.py:144-152`）仍能把「番茄」链回已删的 `ING_00001` → 答复里这条实体没有子图，`/kg/subgraph` 404。这正是记忆里「有别名、有边、没节点」那类孤儿。
- **进程内别名缓存**：`kg/normalize.py:125-148` 的 `all_aliases` 带缓存，注释自己写着「改完 `dict_alias` 要重启进程或 `refresh=True` 才生效」。删除接口和 §四 第 3 步的 `sync_nutrients`（补维C 别名）都在改这张表，服务不重启就还是老字典 —— 第 128 行的验证会莫名其妙失败。

顺带一条：后台查询必须复用 `EntityLinker` / `AliasStore`，不要另写一套精确匹配 SQL，否则会出现「后台查得到、前台查不到」这种最难排查的偏差。

### 3.3 营养边是「合并」，不是「替换」

`kg/merge.py:286-300` 是 `MERGE ... SET`，`relation_key` 相同的边被覆盖，**多出来的边不会消失**。重录把「维生素C 20mg」删掉，图里那条边仍在。

要显式「先按 `(src, CONTAINS_NUTRIENT)` 删掉本食材旧营养边，再写新的」，并且放在同一个 session 里，中途失败不能留下一个没有营养的食材。

另外 `amount` 未必是数值 —— `kg/subgraph.py:104-116` 就有一套从 claim_text 回捞的兜底，这说明历史数据里有字符串。所以写之前要统一成数值、读的时候 `toFloat(r.amount)`，否则「替换」会写出排序排不动的字符串。

### 3.4 重录改写 provenance，回滚会误删

`_STICKY_PROPS`（`kg/promote.py:53-55`）含 `provenance`，「重录允许覆盖」等于把节点归属改成本次重录的 bundle。而 `rollback()`（`kg/merge.py:334` 起）按 `provenance` 删「这一批新建的节点」—— 重录之后这个节点在它眼里就是本批新建的。一次回滚 = 食材节点 + 全部搭配/禁忌边消失，比原始那次误删更难发现。

修法二选一：重录候选打 `re_record` 标记、`rollback` 跳过；或者重录时**保留原 `provenance` 不动**。后者更简单，建议按后者写进 §二。

## 4. 应该补的

### 4.1 删除要留墓碑

删掉别名行 = 词表不再认识这个词。下次有人问「番茄」，会走 `register` 重新进未收录队列，甚至被重新抓取导入 —— 删了又回来。

建议在 `kg_ingredient_request` 写一条 `status='rejected'`、`reject_reason='deleted_by_admin'`，`register()` 命中即直接返回 `rejected`。计划里完全没有这一节。

### 4.2 LLM 兜底少算了四件事

- **登记上限是 2 不是 4**：`graph/nodes.py:162-171` 的 `_eligible_terms` 只取前两个。LLM 拆出猪脚/天麻/当归，第三个永远不会登记，答复却可能说「已反馈」。要么把上限提到 3 并写明，要么只拆 2 个。
- **会撞上旧断言**：`tests/test_web_answer.py:118` 现在断言 `extract_candidates("猪脚天麻当归煲汤合适吗") == ["猪脚天麻当归"]`，也就是把当前 bug 固化了。不改这条，新实现一上线测试就红。
- **要可注入**：`EntityLinker.__init__` 已有 `llm` 参数、`llm/pool.py` 有 `ModelRouter`，走这条路才能写「拆开」「模型不可用保持原状」两条用例；现在计划里没有测试节。
- **要能复核**：拆出来的词落日志或表，否则错拆（把天麻拆成「天」「麻」）没人发现。超时给具体数字（建议 2～3s），失败即放弃 —— 「`/chat` 不能 500」这句是对的，但没写怎么保证。

### 4.3 营养排序的取数口径

- 排序在 Neo4j 做，`toFloat(r.amount)`。没有 `amount` 的按计划单独一段，但要注意 `kg/subgraph.py` 的回捞在 Python 侧，进不了 Cypher 排序 —— 主序只能认边上的数值，或者在写边时补齐。
- 单位取 `n.unit` 还是 `r.unit` 要定一个。现有 `kg/query.py:56-63` 是「`n.unit` 展示、`r.amount` 取数」，跟这个走。
- 分页要总数：`count(*)` 与 `SKIP/LIMIT` 是两次查询，别只 `LIMIT`。
- 维C 别名加在 `data/crosswalk/nutrient_map.csv` 第 2 行的 `aliases`（现在是空），跑 `sync_nutrients` 同步图节点 + `dict_alias`；`kg/nutrients.py` 不用改（它按「中文名,单位」认）。`tests/test_importer.py::test_nutrient_map_csv_matches_resolver` 守的就是这种漂移，跑一下。

### 4.4 后台图组件是重写，不是抽取

§3.4「把这套布局抽成后台一个小组件」抽不出来：前台 `frontend/assets/app.js:622` 是 vanilla JS 手写 SVG（整文件 1437 行），后台是 Vue 3 + Element Plus，没有共享构建层。措辞改成「在后台重写一个等价的 SVG 布局组件，节点/边画法与配色保持一致」，否则实施时会去找一个并不存在的共享模块。

需求只要求 2 个食材，`compare_many` 支持 2～4，不要顺手把上限放开到 4。另外 `sg.resolve` 会走字级模糊，可能「猜中」错的食材，图上要标出命中来源（精确 / 别名 / 模糊），模糊命中让人自己确认。

### 4.5 页面、路由、分页

- 后台既没有食物页也没有对应路由：`admin-web/src/router.js` 要加路由、`Layout.vue:6-12` 要加菜单，计划只说「做后台一个页面」。
- 候选列表是**单选状态筛选**（`Candidates.vue:5-8` + `?status=`），所以「按行判断」实际等价于「按当前筛选判断」，照计划做没问题；顺手把 `limit=50` 无分页补上，否则跟 15/页 的查询页交互不一致。
- 需求 2.2 的 15/页要自己上 `el-pagination`。后台现有列表都没有分页控件，没有样板可抄。

## 5. 施工文档还缺什么

| 缺 | 为什么现在补 |
|---|---|
| 接口契约（路由、入参、响应、错误码） | `/admin` 前缀自带 `require_admin`（`api/admin.py:34`），新接口照这个走；不写清楚，实施时会各发明一套返回结构 |
| 迁移编号 | 重录标记要落库（`kg_food_bundle.re_record` 或 `kg_node_candidate.re_record`），按现有序列是 `024_*.sql` |
| 删除审计 | 破坏性操作要留 who/what/when/边数。现有 `kg_request_action` 只覆盖请求侧，食物删除没有对应表 |
| 测试清单 | 哪些断言、哪些必须 `ALLOW_GRAPH_TESTS=1`（图用例默认跳过，测试与生产共用一个 Neo4j 库）。第 128 行只说「在服务器上验证」，没写跑什么 |

§五 第 3 个拍板点（公开 `/import.html` 是否保留）建议保留，但现在只在 §3.1 提了一句，应当写进结论或施工步骤，避免实施时被当成遗漏。

## 6. 修订顺序（给下一版）

1. §二：粘性属性下沉到 `merge_nodes`；种子名称/分类默认不可改，重录靠 `re_record` 放行。同时在 §五 加「普通导入是否允许改种子名」的拍板点。
2. §二：营养边改「删旧写新」，`amount` 统一数值。
3. §二：重录保留原 `provenance`（或 `rollback` 跳过重录节点）。
4. §二：删除补三件事 —— Milvus 删除或异步重建、别名缓存 `refresh`、墓碑。
5. §3.3：补 `toFloat`、`n.unit`、总数查询。
6. §3.4：措辞改成「后台重写等价组件」。
7. §3.6：补登记上限 2、旧断言要改、LLM 可注入、拆词审计、超时数字。
8. 新增一节「接口契约 + 迁移 024 + 删除审计 + 测试清单」。

## 7. 可以直接照做的

- 首页删 `frontend/index.html:70` 一行，`/import.html` 与 `/kg/import/*` 保留。
- 候选关系按钮按 §3.5 的表：`merged` / `rejected` 全隐，`approved` 只留驳回，`sensitive_block > 0` 仍禁通过。
- 后台食物查询用 `AliasStore` + `sg.neighborhood`，**不调** `POST /kg/compare`（`api/kg.py:184`，第 207 行会把未收录词写进用户队列）—— 这条判断很关键，务必保留。
- 单条删除不用 `rollback()`（`kg/merge.py:334` 起，`provenance` 前缀匹配的坑），按 `canonical_id` + `DETACH DELETE`；共享 `Nutrient` 不动；批量上限 20。
- 重录不先 `DETACH DELETE`、不走 USDA —— 对，中文食材 USDA 收不到，搭配/禁忌边删了就填不回来。
- 已 `merged` 的 bundle / candidate 保持 merged 不动 —— 对，而且 `_upsert_ingredient_candidate`（`kg/promote.py:259-267`）本来就会把同 cid 的候选行改回 `approved`，重录能重建节点正是靠这条 UPDATE。实现时别把它改成 `DO NOTHING`，否则重录会「PG 成功、图里没有」。
