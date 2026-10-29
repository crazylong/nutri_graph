# 「未收录食物」收录渠道与后台工作台改造（v5）

> 来源：用户提问「未收录食物，除了导入，还能怎么收录？」+ 截图 `/console/requests`
> 分析依据：逐条对照仓库实际代码（下文给出 `文件:行号`）
> 状态：**v5.3 / 已解冻，计划到此定稿（2026-10-29）** —— v4 已落地、v3.1 已提交；**施工口径只看 §十三**，其余各节是过程记录
> 说明：本文档只做问题诊断与执行计划，**不含代码实现**
>
> 一句话结论：**这一页不是"收录入口"，是一张只读报表**。真正能收录的三条链路（提问登记 / 表格导入 / 手工录入）在代码里都已经存在，但页面上只连了一条，另外两条在别的地方，而且互不连通 —— 所以运营的主观感受就是"除了导入没别的路"。

> 📖 **阅读指引（先读这段）**
>
> 1. **当前状态**：v4（web 搜索兜底）已实现、v3.1 已提交，本文已解冻，可以开工。
> 2. **正文 §四–§六 是冻结旧稿，不要照着写代码**。里面已知有 19 处事实性错误（`n.name` 查不到食材、`Ingredient:123` 格式不存在、迁移号撞车、断言 1/3/8 不可达等）。
> 3. **施工只看 §十三**。它是三轮评审之后收敛出来的唯一口径，已合并 §9.5 勘误、§十一（v4 落地订正）、§十二（第二轮）的全部结论。
> 4. 各节用途：§九 = 勘误表（19 条，可查"我原来写错了什么"）；§十一 = v4 落地后的依赖体检；§十二 = 第二轮五处；**§十三 = 定稿施工口径**。

---

## 零、结论摘要

| 项 | 判定 | 一句话结论 |
|---|---|---|
| 「只能导入」是不是真的 | ⚠️ **一半是真的** | 代码里有 4 条收录通道，页面上只露出 1.5 条（导入 + 一个只登记不补录的"手工登记"） |
| 这一页的设计定位 | ❌ **错了** | 做成了只读报表（只有筛选 + 一个全局按钮），应该是"待处理工作台" |
| 队列里的数据质量 | ❌ **有噪音** | 「章鱼营养价值」「奇亚籽每天」不是食物名，是抽取层没切开（§三 P3） |
| 队列里的状态可信吗 | ❌ **不可信** | 6 条全是 `queued`，而 `queued` 在现有代码里**永远不会变**（§三 P4） |
| 最该先做的一件事 | ✅ **反查 + 分类** | 先把"图谱里其实有、只是名字对不上"的挑出来挂别名，比补录 100 条新数据更能提覆盖率 |
| 与 v4 的关系 | ⚠️ 互补 | v4 解决"C 端当轮有没有答案"，v5 解决"运营能不能把数据补进去"；v4 的 R1（`queued` 永不变）在 v5 里升级为 P4 必修 |

**范围**：把 `/console/requests` 从只读列表改造成收录工作台；新增「反查归类 → 挂别名 / 补录 / 驳回」三段动作；修复状态机与抓取调度；治理抽取层噪音。
**不做**：不直写 Neo4j（主图写入口仍然只有 `kg.merge`）；不做 OCR/拍照（排到 v6）；不做 C 端上报。

合计 **4.5～5 人日**。

---

## 一、先看这一页：6 条数据其实是三类东西

截图里的 6 行（另有 1 行被裁切），按"该被怎么处理"可以分成三类：

| 行 | 次数 | 实际性质 | 正确处理 | 现在能做的 |
|---|---|---|---|---|
| 天麻 | 1 | **真缺失**（药食同源，USDA 基本不收） | 走权威库/典籍补录 | 只能驳回 |
| 猪脚 | 1 | **疑似假缺失**（图谱里大概率有「猪蹄」） | 挂别名 | 只能驳回（字级相似度算不出，见 P5） |
| 牛油果 | 8 | **疑似假缺失**（8 次都问、都没收录，更像名字没对上） | 反查 → 挂别名 或 补录 | 只能驳回 |
| 章鱼营养价值 | 1 | **噪音**（问句片段，非食物名） | 判为噪音，批量清理 | 只能驳回 |
| 奇亚籽每天 | 3 | **噪音 + 真词混合**（应切成「奇亚籽」） | 切开后重走一遍 | 只能驳回 |
| 羽衣甘蓝 | 3 | **疑似假缺失**（常见蔬菜，图谱里很可能有） | 反查 → 挂别名 | 只能驳回 |

> 三类数据混在一张表里、用同一个"驳回"动作处理，是这一页最根本的设计问题。
> 用"导入"解决假缺失会**造出重复节点**（「猪脚」和「猪蹄」两个 Ingredient），这比不收录更糟 —— 正是 `kg/request.py:12-15` 文件头明确要避免的情况。

---

## 二、现状核对：一条食物到"被收录"到底有几条路

### 2.1 四条通道，代码里都已存在

| # | 通道 | 触发方 | 代码位置 | 落到哪 | 现在可达吗 |
|---|---|---|---|---|---|
| A | 用户提问 → 自动登记 | C 端 chat | `graph/nodes.py:156 uncatalogued_node` → `kg/request.py:186 register()` | `kg_ingredient_request` + `kg_fetch_task` | ✅ 在跑 |
| B | 表格批量导入 | 运营 | `api/admin_import.py:213 commit` → `kg/import_bundle.py:224 stage_bundles` | `kg_food_bundle` → 补录审核 | ✅ 在跑 |
| C | 手工录入（单条填表） | **C 端** | `api/food_import.py:385 POST /food-import/manual` | `kg_food_bundle(manual_entry)` → 补录审核 | ⚠️ 存在，但**不在后台**，且按 IP 限流（`config.py:297` = 40 条/天） |
| D | 别名回流 | 系统/运营 | `kg/request.py:264 add_candidate` → `dict_alias_candidate`；`kg/promote.py:550` 合并时回写 `dict_alias` | `dict_alias` | ⚠️ 只写不审：`api/admin.py:446` 是**只读**接口，没有"确认"动作 |

### 2.2 关键结构性发现：B/C 和 A 在数据上是接不上的

```
C 端提问 ──► kg_ingredient_request ──► kg_fetch_task ──► USDA ──► kg_food_bundle ──┐
             (只记需求热度)              (手动命令)                          │
                                                                              ├──► kg.merge ──► Neo4j
运营 Excel ─────────────────────────────────────────────► kg_food_bundle ─────┘
运营手工填表 (/import.html) ─────────────────────────────► kg_food_bundle ─────┘
```

- A 通道（提问登记）**只能靠 USDA 抓取**才能产出 bundle；USDA 抓不到的词（天麻、猪脚）就永远停在 `queued`。
- B/C 通道能产出完整 bundle（含营养素），但**入口在"批量导入"和 C 端 import 页**，运营看着「天麻」这一行，没有办法从这里直接给他填营养素。
- 结果：**A 里的每一条，运营都只有两个动作：驳回，或者去另一个页面重新打一遍名字**。这就是"除了导入没别的路"的真实成因。

### 2.3 「手工登记」按钮做的不是收录

`api/admin.py:405-443 create_food_request` → `register(..., count_hit=False)`：

- 只写 `kg_ingredient_request` + 建一个 `kg_fetch_task`（`kg/request.py:332-337`）
- **不能填食物名以外的任何字段**（`ManualFoodRequest` 只有一个 `name`，`api/admin.py:306-307`）
- 对图谱零影响

所以这个按钮更准确的名字是**"代用户登记一次需求"**，不是"录入食物"。文案与能力不匹配，是"看起来有入口、实际没用"的直接原因。

---

## 三、问题清单

### P1（P0）页面没有"收录"这个动作，只有"驳回"

`admin-web/src/views/Requests.vue:14-24` 表格无操作列；唯一的写操作在抽屉里：`:42-43` 只有"驳回理由 + 驳回按钮"。

一个名为「未收录食物」的页面，**没有任何一个按钮能把这个食物变成"已收录"**。这是最刺眼的问题。

### P2（P1）「归一名」和「原话」两列内容一模一样

`Requests.vue:15-16` 分别绑 `food_key` 与 `raw_mention`。截图上 6 行两列完全相同（天麻/天麻、猪脚/猪脚…），因为：

- `kg/request.py:126-138 normalize_food_term` 只在命中 `_SUFFIXES`（`:40-44`）/`_PREFIXES`（`:45`）时才剥离
- `_SUFFIXES` 只有 20 个烹饪/形态词（粥/汤/汁/粉/糊…），不含"每天""营养价值"这类
- 剥不动就 `return term, "asis"`，于是 `food_key == normalized == raw_mention`

**两列重复 = 归一化对这批数据完全没生效**，等于白占一列宽度，也让人看不出"该归一成什么"。

### P3（P1）抽取层在制造噪音，且噪音会进队列

「章鱼营养价值」「奇亚籽每天」能通过全部闸门，逐个环节都能定位：

| 环节 | 位置 | 为什么没拦住 |
|---|---|---|
| 停顿词切分 | `kg/request.py:51-60 _BREAK_WORDS` | 有"多少/什么/做法"，**没有**"每天/每日/营养价值/营养成分表/热量高吗" |
| 非食物词表 | `kg/request.py:63-68 _NON_FOOD_TERMS` | 有"营养""营养成分"，**没有**"营养价值"（整串匹配，不做子串判断） |
| 长度粗筛 | `kg/request.py:70 _TERM_RE` = `2-8 字` | 「章鱼营养价值」6 字、「奇亚籽每天」5 字，**都合规** |

后果：队列里混着永远补不进来的伪食材，运营每处理一条都要先做一次"这是不是食物"的判断，成本远高于处理真数据。

### P4（P0）状态机断裂：`queued` 永远不会变，而且抓取是手动命令

- `kg/fetch.py:158 _set_request_status` 全仓**只有一个调用点**：`:334`，成功路径
- 失败路径 `:294 / :304 / :323 / :329` 只 `_finish(task)` 改任务表，**不动 request 状态**
- `deploy/systemd/` 只有 `nutri-graph.service`，**没有 `run_fetch` 的 timer**；`docs/deploy.md:157` 写的是手工 `python -m scripts.run_fetch --limit 3`

结论：截图里 6 条全是"已登记，等待处理"，**不是"正在排队"，是"已经卡死"**。用户看到的"审核通过后就能查到"永远不会兑现 —— 这一条是 v4 的 R1，在 v5 里必须升级为必修，否则这个页面做再多按钮，数据也不会自己往前走。

### P5（P1）假缺失识别不出来：字级相似度对"同义换字"无能

`kg/textsim.py:42-53 char_similarity`：包含关系 → 长度比；否则 `1 - lev/max_len`。

| 词对 | 编辑距离 | 得分 | 阈值 0.62（`config.py:285`） | 结果 |
|---|---|---|---|---|
| 黑豆 / 黑大豆 | 1 | 2/3 = 0.67 | 通过 | ✅ 能提别名建议 |
| 猪脚 / 猪蹄 | 1 | 1-1/2 = **0.50** | 不通过 | ❌ 直接开新登记 |
| 牛油果 / 鳄梨 | 2 | **0.33** | 不通过 | ❌ 直接开新登记 |
| 番茄 / 西红柿 | 3 | **0.00** | 不通过 | ❌ 直接开新登记 |

`char_similarity` 只认"长得像"，不认"意思一样"。**中文食材里最常见的恰恰是换字同义**（脚/蹄、番/西、凤/菠、提子/葡萄），这类全部漏掉，且无任何提示。这是 P1 级的召回漏洞，也是"图谱有 4000+ 食物、队列还在涨"的原因之一。

### P6（P2）「已收录」判定只查 PG 别名表，不查图谱本身

`kg/request.py:230-235` 只调 `store.exact_lookup(term)`，读的是 `dict_alias` 表。

`dict_alias` 的写入点只有三处：`scripts/load_crosswalk.py:70`、`scripts/sync_nutrients.py:108`、`kg/promote.py:550`（合并时回写）。

于是只要节点不是经 promote 进来的（`scripts/backfill_nutrients.py --restore` 抢救路径、直接 Cypher 建节点），**图谱里有、别名表没行 → 会被判成"未收录"**。判定依据和事实来源不是同一个地方，这是设计缺陷，不只是数据问题。

### P7（P2）「手工录入」能力存在于错误的入口

`api/food_import.py:385` 的手工录入是后台最需要的能力，但它：

- 挂在 C 端 `/import.html` 上（`api/food_import.py:2-8` 文件头写明：挂在 `/kg/import` 下、不登录也能用，后台导航里没有入口）
- 按客户端 IP 限流（`:397` `_quota_used(ip, ...)`），运营在办公室一条条填，40 条/天就满
- 必填字段是完整营养素模板（`importer.COLUMNS`），对着一张只有"天麻"两个字的登记，运营要自己去查全部数值

### P8（P3）列表交互缺一整套基础能力

| 缺什么 | 现状 | 应该 |
|---|---|---|
| 排序 | `api/admin.py:358` 固定 `ORDER BY last_requested_at DESC` | 支持按 `hit_count` 降序（高频优先） |
| 时间显示 | `Requests.vue:23` 直接渲染 ISO 串，截图显示 `2026-10-29T06:15:47.889282+08:00` | 格式化为"3 分钟前" |
| 批量操作 | 无 `el-table` 选择列（`Requests.vue:14`） | 勾选 + 批量挂别名/批量驳回 |
| 来源区分 | `source` 列纯文本（`chat`/`admin`） | 图标 + 来源分布统计 |
| 疑似匹配预览 | 抽屉里只有 8 个字段的纯文本（`Requests.vue:34-41`） | 进抽屉时自动反查并展示候选 |

---

## 四、设计决策

### D1 —— 收录拆成三种动作，必须先判后动

**决策**：每一条登记，先反查归类，再给动作；不允许"一键补录"直接建节点。

| 判定结果 | 动作 | 落地 |
|---|---|---|
| **已存在**（别名命中 / 图谱同名 / 同义确认） | 挂别名 | 写 `dict_alias` + `dict_alias_candidate.status='approved'` + 该条 request 置 `approved` |
| **疑似存在**（相似度高但不确定） | 人工确认后挂别名 | 同上，抽屉里展示候选让运营点选 |
| **不存在** | 补录（填营养素 / 触发权威库抓取） | 走 `kg_food_bundle` → 补录审核 → `kg.merge` |
| **不是食物** | 驳回（理由选"非食物/问句片段"） | `status='rejected'` + 理由枚举，供后续统计 |

**理由**：用"补录"解决假缺失会造重复节点（`kg/request.py:12-15` 已明确记录这个教训：黑大豆/黑豆粥 开三条任务 → 重复节点）。先判后动是唯一能同时提覆盖率又不污染图谱的顺序。

### D2 —— 不新增主图写入口

所有补录仍然落到 `kg_food_bundle`，经「补录审核」页 approve 后由 `kg.promote.approve_bundle` → `kg.merge` 写 Neo4j。

**不允许**在 admin 里写一条 `MERGE (n:Ingredient ...)`。理由：`api/admin.py:10-11` 已经把"审核动作只改状态、不直接写主图"定为约定；多一个写入口，`kg/merge` 的证据链/批次回滚（`kg/merge.py:283 rollback`）就管不住它。

### D3 —— 补录表单复用导入校验，不新写一套

`api/food_import.py:413` 调 `importer.validate_row` 是唯一的行校验入口，文件头 `:389-392` 写明了理由（规则只有一份）。

**决策**：新增 `POST /admin/kg/requests/{id}/bundle`，内部同样走 `importer.validate_row` + `import_bundle.stage_bundles(..., source_key='manual_entry')`，只是把 IP 限流换成管理员鉴权（`require_admin` 已在 router 级挂好，`api/admin.py:27`）。

副作用：`migrations/015` 的 `uq_open_import_bundle` 唯一索引（`food_key, source_key` where status in draft/pending_review/approved）会自动保护"同一食材只留一份未结 bundle"，不用额外写去重。

### D4 —— 反查分三层，最便宜的放最前

| 层 | 手段 | 成本 | 覆盖 |
|---|---|---|---|
| L1 | `AliasStore.exact_lookup`（`kg/normalize.py:104`） | 一次索引查询 | 别名表精确命中 |
| L2 | 图谱点名 `MATCH (n:Ingredient) WHERE n.name CONTAINS $kw`（限 20 条） | 一次图查询 | 节点在但别名表缺行（补 P6） |
| L3 | `AliasStore.fuzzy_lookup`（`kg/normalize.py:178`）+ `char_similarity` | 全量内存扫描（几百到几千条） | 长得像（黑大豆→黑豆） |
| L4 | **同义判定**（LLM 或别名映射表） | 一次模型调用，只在 L1-L3 全空时触发 | 意思一样（猪脚→猪蹄）—— P5 的洞 |

**L4 要不要做模型调用**：建议做，但**只返回候选不给结论**（`{"canonical_name": "...", "confidence": 0..1}`），必须人点确认。理由是"同义判定"正是模型最容易自信胡说的地方（把「天麻」判成「山药」这类），而这里一旦判错就是往图谱里挂错别名，代价比人工多看一眼高得多。

### D5 —— 抓取链路要么有调度，要么承认它是手动工具

P4 的两个修法必须一起做，只做一半没有意义：

1. **状态推进**：`kg/fetch.py:294/304/323/329` 四个失败出口补 `_set_request_status(request_id, 'rejected'|'queued')`，让 `queued` 不再是终点。
2. **调度**：`deploy/systemd/nutri-graph-fetch.{service,timer}`（每 10 分钟 `run_fetch --limit 3`），或明确在页面上给一个"立即抓取"按钮把 `run_fetch` 变成运营可点的动作。

**决策**：默认做 (2) 的第二种 —— 页面按钮 + 手动兜底；timer 作为可选配置项（`FOOD_FETCH_AUTO=true`）。理由：抓取烧的是 USDA 的 key 额度（`FOOD_FETCH_DAILY_BUDGET=50`，`config.py:314`），无人值守地自动烧到上限，不如让运营在有需求时点一下，可控。

### D6 —— 新渠道的优先级顺序（本期只做前两条）

| 渠道 | 价值 | 本期 |
|---|---|---|
| 反查归类（L1-L4） | 直接提覆盖率，零脏数据风险 | ✅ |
| 后台补录表单（复用导入校验） | 让 queue 里的每一条都能就地补完 | ✅ |
| 拍照 / OCR（小票、包装背标） | C 端最高频的真实场景，但需要模型 + 图片存储 | ⏳ v6 |
| C 端"提交这个食物"上报页 | 回流质量高（带原话 + 图） | ⏳ v6 |
| 权威库批量反查（中国食物成分表 / OpenFoodFacts） | 能一次性清空假缺失，但依赖 `scripts/import_fcd.py` 先跑通（v4 的 R2） | ⏳ 依赖 v4-R2 |

---

## 五、实施方案

### 5.1 新增文件

| 文件 | 职责 | 要点 |
|---|---|---|
| `kg/lookup.py` | 反查编排 | `match(term) -> list[MatchHit]`；L1→L2→L3→(L4 可选)；每层命中即短路；单层超时不影响其它层 |
| `kg/synonym.py` | 同义判定（L4） | `suggest(term, candidates) -> dict \| None`；**只返回候选，不写库**；模型不可用时返回 `None`，整条链路照常跑 |
| `migrations/020_request_triage.sql` | 归类结果 + 动作审计 | 见 5.4 |
| `tests/test_request_triage.py` | 单测 | 覆盖 §六 全部断言 |

### 5.2 改动文件

| 文件 | 位置 | 改动 |
|---|---|---|
| `api/admin.py` | `:328` `GET /kg/requests` | 增 `sort` 参数（`recent`\|`hits`）；返回体补 `triage` 字段（最近一次反查结论） |
| `api/admin.py` | 新增 `GET /kg/requests/{id}/matches` | 调 `kg.lookup.match`，返回候选列表 + 每层来源 |
| `api/admin.py` | 新增 `POST /kg/requests/{id}/alias` | 体 `{canonical_id, alias}` → 写 `dict_alias` + request 置 `approved` |
| `api/admin.py` | 新增 `POST /kg/requests/{id}/bundle` | 体 = 导入模板字段；内部 `importer.validate_row` + `stage_bundles`，见 D3 |
| `api/admin.py` | 新增 `POST /kg/requests/batch` | 体 `{ids, action, reason}`；action ∈ `reject`\|`alias`\|`ignore_noise` |
| `api/admin.py` | `:385-402` `reject` | 理由改为枚举 + 自由文本：`not_food`\|`noise`\|`duplicate`\|`cannot_verify`\|`other` |
| `api/admin.py` | `:446-468` `alias-candidates` | 补 `POST /kg/alias-candidates/{id}/approve`（当前只读，没有确认动作） |
| `kg/request.py` | `:51-60 _BREAK_WORDS` | 补：`每天 每日 一天 每周 早上 晚上 空腹 饭前 饭后 睡前 适量 营养价值 营养成分表 热量表 能不能吃` |
| `kg/request.py` | `:63-68 _NON_FOOD_TERMS` | 补：`营养价值 营养成分 含量 吃法 搭配 食谱 热量`；判定改为**子串包含**（现在是整串相等） |
| `kg/request.py` | `:141-169 extract_candidates` | 剥离后若长度 > 4 且仍含非食物词 → 丢弃，而不是塞进队列 |
| `kg/fetch.py` | `:294 / :304 / :323 / :329` | 失败出口补 `_set_request_status`（P4） |
| `scripts/run_fetch.py` | `:1` 入口 | 增 `--request-id` 参数，供后台"立即抓取"按钮单条触发 |
| `admin-web/src/views/Requests.vue` | `:14-24` 表格 | 加选择列、加操作列（挂别名 / 补录 / 驳回）、`hit_count` 可排序、时间格式化 |
| `admin-web/src/views/Requests.vue` | `:33-45` 抽屉 | 顶部加「疑似匹配」区（调 `/matches`），命中即给"确认挂别名"按钮 |
| `admin-web/src/views/Requests.vue` | `:47-53` 手工登记 | 保留但改文案为"代登记需求"，与"补录"分开，避免混用 |
| `admin-web/src/views/Requests.vue` | 新增 | 补录表单弹窗（复用 `Import.vue` 的字段渲染，避免两套模板） |
| `admin-web/src/views/Aliases.vue` | — | 加"确认/否决"两个动作（现在纯只读） |
| `api/admin_import.py` | `:127 preview` 附近 | 无需改动，确认补录走的是同一个 `stage_bundles` 即可 |

### 5.3 新增接口清单

```
GET  /admin/kg/requests/{id}/matches
  → { "term": "猪脚",
      "hits": [ { "layer": "graph_name", "canonical_id": "Ingredient:123", "name": "猪蹄",
                  "score": null, "why": "图谱节点名包含「猪」" },
                { "layer": "synonym", "canonical_id": "Ingredient:123", "name": "猪蹄",
                  "score": 0.88, "why": "模型判定，需人工确认" } ],
      "verdict": "likely_exists" }        // exists | likely_exists | not_found | not_food

POST /admin/kg/requests/{id}/alias
  body { "canonical_id": "Ingredient:123", "alias": "猪脚" }
  → { "ok": true, "alias_id": 88, "request_status": "approved" }

POST /admin/kg/requests/{id}/bundle
  body = importer.COLUMNS 全字段（同 /food-import/manual）
  → { "ok": true, "bundle_id": 501, "verdict": "ok"|"warn",
      "warnings": [...], "next": "去补录审核页确认" }

POST /admin/kg/requests/batch
  body { "ids": [12,15], "action": "reject"|"ignore_noise", "reason": "noise" }
  → { "ok": true, "affected": 2 }
```

### 5.4 迁移 `020_request_triage.sql`

```sql
-- 归类结论：一条登记最后一次反查的结果，供列表页直接标注
ALTER TABLE kg_ingredient_request
    ADD COLUMN IF NOT EXISTS triage          TEXT NOT NULL DEFAULT '',
    ADD COLUMN IF NOT EXISTS triage_hit_id   TEXT NOT NULL DEFAULT '',
    ADD COLUMN IF NOT EXISTS triage_at       TIMESTAMPTZ,
    ADD COLUMN IF NOT EXISTS handled_by      TEXT NOT NULL DEFAULT '',
    ADD COLUMN IF NOT EXISTS handled_at      TIMESTAMPTZ;

ALTER TABLE kg_ingredient_request
    DROP CONSTRAINT IF EXISTS kg_ingredient_request_triage_chk;
ALTER TABLE kg_ingredient_request
    ADD CONSTRAINT kg_ingredient_request_triage_chk
    CHECK (triage IN ('', 'exists', 'likely_exists', 'not_found', 'not_food'));

-- 动作审计：谁在什么时候把哪条登记变成了什么
CREATE TABLE IF NOT EXISTS kg_request_action (
    id          BIGSERIAL PRIMARY KEY,
    request_id  BIGINT      NOT NULL,
    action      TEXT        NOT NULL,   -- alias | bundle | reject | ignore_noise | fetch
    payload     JSONB       NOT NULL DEFAULT '{}',
    actor       TEXT        NOT NULL DEFAULT '',
    created_at  TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS idx_req_action_req ON kg_request_action (request_id, created_at DESC);
```

> 为什么单独建审计表而不是复用 `reject_reason`：驳回理由只能表达"否"，表达不了"我把「猪脚」挂到了 `Ingredient:123`"。出问题时需要回溯"这条别名是谁什么时候挂的"，否则 `kg/merge.py:283 rollback` 的批次回滚也救不了别名污染。

### 5.5 页面改版结构

```
┌─ 未收录食物 ────────────────────────────────────────────────────────────┐
│ [状态▾] [关键词___] [查询]        [按次数排序▾]      [代登记需求]        │
│ ─────────────────────────────────────────────────────────────────────── │
│ ☐ 归一名   原话              次数  判定        状态        最近    操作  │
│ ☐ 牛油果   牛油果            8     🔴 疑似已有  等待处理   3h前   [挂别名][补录][驳回] │
│ ☐ 羽衣甘蓝 羽衣甘蓝          3     🔴 疑似已有  ...                     │
│ ☐ 天麻     天麻              1     ⚪ 真缺失    ...                     │
│ ☐ 奇亚籽每天 奇亚籽每天      3     🟡 疑似噪音  ...       [判噪音]      │
│ ─────────────────────────────────────────────────────────────────────── │
│ 已选 2 条  → [批量挂别名] [批量驳回] [批量判噪音]                        │
└─────────────────────────────────────────────────────────────────────────┘

点行 → 右侧抽屉
┌ 登记明细 #408 ─────────────────┐
│ 归一名 牛油果   次数 8          │
│ 原话   牛油果                   │
│ 最近提及 3 小时前（06:15）       │
│ ─────────────────────────────  │
│ 疑似匹配（已自动反查）           │
│  ● 鳄梨 Ingredient:209  图谱点名 │
│  ○ 牛油果(Avocado) Ingredient:88 字级 0.71 │
│  [确认挂别名]                   │
│ ─────────────────────────────  │
│ 或者：补录这条食物 →            │
│ 驳回原因 [非食物▾] [____] [驳回]│
└────────────────────────────────┘
```

---

## 六、验收标准

全部在远程跑（本地无 venv、连不上 PG/Neo4j，见项目铁律）：

```bash
# 在服务器上执行。PG_DSN 指向 nutri_graph_test，口令放在环境里，不写进文档。
/opt/envs/nutri/bin/python -m pytest tests/test_request_triage.py tests/test_food_request.py -q
```

必须成立的断言：

| # | 断言 |
|---|---|
| 1 | `match("猪脚")` 在测试库（含 `猪蹄` 节点 + 无同名别名）下返回 `likely_exists`，且 `hits` 里出现 `猪蹄` |
| 2 | `match("天麻")`（两处都没有）返回 `not_found`，`hits` 为空，**不抛异常** |
| 3 | `match("章鱼营养价值")` 返回 `not_food`（命中 `_NON_FOOD_TERMS` 子串规则） |
| 4 | `POST /alias` 后：`dict_alias` 有 `("Ingredient", cid, "猪脚", "猪脚")` 一行；该 request `status='approved'`；`kg_request_action` 新增一条 `action='alias'` |
| 5 | 同一 `food_key` 已有 `pending_review` bundle 时再 `POST /bundle` → 覆盖原 bundle（`uq_open_import_bundle` 语义），**不产生第二条** |
| 6 | `POST /bundle` 传非法数值（如蛋白质 999g/100g）→ 422 + 字段级 error，与 `/food-import/manual` 的报错文本一致（同一条校验规则） |
| 7 | `POST /batch {action:'ignore_noise'}` → 两条 request 置 `rejected` 且 `reject_reason='noise'`，`kg_request_action` 各一条 |
| 8 | `extract_candidates("章鱼营养价值")` 返回 `[]`；`extract_candidates("奇亚籽每天")` 返回 `["奇亚籽"]`（新增词表后） |
| 9 | `_set_request_status` 在抓取失败路径被调用：mock 一个 `DraftError`，request.status 从 `queued` 变 `rejected`，**不残留 `queued`** |
| 10 | `sort=hits` 时返回顺序按 `hit_count DESC`，截图里的 `牛油果(8)` 排第一 |
| 11 | 无管理员 token 访问上述全部新接口 → 401/403（router 级鉴权生效） |
| 12 | `alias-candidates/{id}/approve` 后 `dict_alias` 有行，且 `dict_alias_candidate.status='approved'` |

---

## 七、风险与不做

### R1（高）挂错别名比不收录更糟

`dict_alias` 一旦写了 `猪脚 → Ingredient:123`，**整个 C 端问答链路都会跟着错**（`graph/nodes.py:97 link_node` 走的就是这张表）。而且 `kg/merge.py:283 rollback` 按 batch 回滚，回滚不了人工挂的别名。

**缓解**：
- 别名写入必须来自"人点确认"，L4 模型结论只作候选
- `kg_request_action` 记录 actor + canonical_id，出问题能按 request 反查污染范围
- 建议加一条 24 小时内的"撤销"（`DELETE FROM dict_alias WHERE ... ` + 恢复 request 状态），不做全量回滚

### R2（中）L4 同义判定会引入模型成本与延迟

只在 L1-L3 全空时触发，且限时 5s。默认 `TRIAGE_SYNONYM_ENABLED=false` 灰度，打开后观察 `kg_request_action` 里 alias 的撤销率。

### R3（中）抽取层改词表会影响 C 端话术

`_BREAK_WORDS` 同时被 `register()` 和 `uncatalogued_node` 用。加词后可能出现"本该登记的现在不登记了"，用户看到的提示从"已登记 #408"变成"没识别出食材"。

**缓解**：新增词只加"时段/频次/复合营养词"，不碰单字虚词（`kg/request.py:73-76` 已经踩过"单字虚词误伤多宝鱼/无花果"的坑，注释写得很清楚）。改完必跑 `tests/test_food_request.py` 与 `tests/test_retrieval_plan.py`。

### R4（低）批量动作误操作

批量驳回不可逆（只改状态，数据还在，可人工改回）。批量挂别名**不做**，只能一条条确认 —— 别名是全局生效的，批量选错行等于批量污染。

### 明确不做

- ❌ 不在 admin 直写 Neo4j（D2）
- ❌ 不做"一键全部补录"（D1）
- ❌ 不做 OCR / 拍照上传（v6）
- ❌ 不做 C 端食物上报页（v6）
- ❌ 不改 `uncatalogued_node` 的 C 端话术结构（v4 已定，避免冲突）
- ❌ 不做中国食物成分表批量导入（依赖 v4-R2，独立需求）
- ❌ 不做"自动挂别名"（任何形式的无人确认写入）

---

## 八、工作量与发布

| 阶段 | 内容 | 人日 |
|---|---|---|
| M1 反查 | `kg/lookup.py`（L1-L3）+ `/matches` + 迁移 020 | 1.0 |
| M2 动作 | `/alias`、`/bundle`、`/batch`、`kg_request_action` 审计 | 1.0 |
| M3 前端 | 表格选择列 + 操作列 + 排序 + 抽屉疑似匹配 + 补录表单 | 1.5 |
| M4 状态机 | `kg/fetch.py` 四处失败出口 + `run_fetch --request-id` + 页面"立即抓取" | 0.5 |
| M5 噪音 | `kg/request.py` 词表 + `extract_candidates` + 存量清理脚本 | 0.5 |
| M6 L4 | `kg/synonym.py` + 灰度开关（可与 M1 并行，默认关） | 0.5 |
| — | **合计** | **5.0** |

**发布顺序**：迁移 → M1/M2 后端（接口先上，页面不动）→ M3 前端 → M4/M5 → 打开 `TRIAGE_SYNONYM_ENABLED` 观察一周。

**回滚**：
- 前端回滚 = 换回旧 `Requests.vue`（旧接口全部保留，未删除任何字段）
- 后端回滚 = 新接口不挂载即可，`triage` 字段为空不影响旧逻辑
- 别名污染 = 按 `kg_request_action` 反查 + 手工 `DELETE`

**存量清理（M5 一次性脚本，`scripts/clean_request_noise.py`）**：
按新词表重跑 `extract_candidates`，把 `food_key` 含非食物词的行走 `status='rejected', reject_reason='noise'`。**先 `--dry-run` 打印将要改的行**，确认后再执行 —— 这是生产表，参照 `scripts/backfill_nutrients.py` 的 dry-run 惯例。

---

## 九、评审复核结论（2026-10-29）

> 对象：`docs/review/review_v5.md`（127 行）
> 方法：逐条回到仓库当前代码复核，不用评审自己的结论当证据。
> **总结论：评审成立，可以据此改计划。** 抽查 22 条实质判断 —— 18 条与代码一致，1 条结论过度，2 条行号偏差，1 条方案取舍可商榷；另发现评审漏掉 2 点、评审与 v4 的硬冲突 1 处。
>
> ⚠️ 复核期间发现**代码在动**：`config.py` 348 → 355 行、`migrations/020_turns.sql` 新增、`graph/nodes.py` 的 `uncatalogued_node` 从 `:114` 移到 `:156`、`plan_v4.md` 从 383 行涨到 442 行。所以本文所有行号都只代表"写这份文档那一刻"，**开工前必须按符号名重新定位**（见 §10 第 3 项）。

### 9.1 成立、必须采纳

| 评审意见 | 我的复核证据 | 影响本文 |
|---|---|---|
| 迁移号改成 021 | `migrations/020_turns.sql` 确实存在（我写计划时目录只到 019） | §五 5.4 |
| config 行号全错 | 现文件 355 行：0.62 在 `:292`、40 条/天在 `:304`、抓取预算 50 在 `:321`。数值对、行号错 | §三 P5、P7 |
| 牛油果/鳄梨 是 0.00 | 无公共字，lev=3，`1-3/3=0`。我写的"距离 2 / 0.33"是算错 | §三 P5 表 |
| 漏了羽衣甘蓝/甘蓝 | 包含关系 → 2/4 = **0.50**，同样过不了 0.62 | §三 P5 表 |
| Ingredient 没有 `name` 属性 | `kg/schema.py:83` 是 `canonical_name`；`name` 只给 Nutrient/Population/Symptom/Taboo 用。我的 L2 `n.name CONTAINS` 一个食材都查不到 | §四 D4、§五 5.3 |
| `canonical_id` 不是 `Ingredient:123` | `kg/schema.py:161 make_canonical_id` → `f"{prefix}_{num:05d}"`，即 `ING_00001` | §五 5.3 样例 JSON |
| 断言 1 不可达 | L2 用「猪脚」CONTAINS 碰不到「猪蹄」；L3 = 0.50 < 0.62；L4 默认关。三条路全断 —— 这条断言是自相矛盾 | §六 断言 1 |
| `/alias` 必须走 `sync_aliases` | `kg/promote.py:528` 已有"绝不抢别人的别名"闸；`dict_alias` PK 是 `(entity_type, alias_norm, canonical_id)`（`migrations/002_business.sql:21`），裸 INSERT 能让一个词挂两个节点 | §四 D1、§五 5.2 |
| 写完别名必须刷缓存 | `graph/nodes.py:100` 用 `get_alias_store()` 单例；`all_aliases(refresh=True)` 在 `kg/normalize.py:125`。不刷新 = 后台显示"已收录"、用户下一句仍进未收录 | §四 D1 |
| 别名确认后抓取任务仍在跑 | `kg/fetch.py:334` **无条件**改写 request 状态，已 `approved` 的会被打回 `pending_review` | §四 D1、D5 |
| 补录后登记行不动 | `kg/import_bundle.py:177` 插入 bundle 时 `VALUES (NULL, ...)`，`request_id` 恒空 | §四 D3 |
| 唯一索引挡不住 USDA + 手工两份 | `migrations/015` 的索引 `WHERE source_key IN ('excel_import','manual_entry','fcd_crawler')`，不含 `usda_fdc`（`kg/fetch.py:191`） | §四 D3 |
| 手工营养素表单不在 `Import.vue` | `Import.vue` 只有上传；真表单在 `frontend/assets/import.js:451` 起，按 `/kg/import/meta` 的 columns 生成 | §八 M3 工时 |
| 断言 8 自相矛盾 | 把「营养价值」加进 `_BREAK_WORDS` 后先切分，`extract_candidates("章鱼营养价值")` 返回 `["章鱼"]`，不是 `[]` | §六 断言 8 |
| `_NON_FOOD_TERMS` 不能改子串 | `kg/importer.py:665-667` 复用同一个集合且用 `in` 整词判定。只改 `is_plausible_food` → 导入和提问两套规则分叉；子串还会挡掉「水果玉米」「营养面」 | §五 5.2 |
| `run_fetch` 已有 `--task-id` | `scripts/run_fetch.py:6` 用法里就写着 | §五 5.2 |
| 可重试失败不能打 `rejected` | `MAX_ATTEMPTS = 3`（`kg/fetch.py:34`）；`register()` 的 `ON CONFLICT`（`kg/request.py:316-321`）**不恢复 status** → `rejected` 是终态，这条食物再没有第二次抓取机会 | §四 D5、§六 断言 9 |
| 批量挂别名与 R4 打架 | 我自己在 §七 R4 写了"批量挂别名不做"，却在 §五 5.5 线框和 5.3 的 `action` 枚举里留了它 | §五 5.2/5.5 |
| 测试命令带明文口令 | 本文 §六 直接写了含密码的 DSN；`plan_v3.md:710` 的写法只有"库指 `nutri_graph_test`" | §六 |
| 单 worker，别名缓存进程内 | `deploy/systemd/nutri-graph.service` 的 `--workers 1`，注释里写明"别名缓存、模型池状态都在进程内" | §四 D1 |

### 9.2 结论过度、按修正版执行

**(1)「挂别名后可能再造一个节点」——不会。**
`kg/fetch.py:169 _alias_conflict` 会查 `dict_alias`，命中即把冲突传给 `verify(conflict_alias=...)`，`kg/verify.py:89-91` 把它变成**硬否决**，bundle 带 `hard_veto` 不会被 `kg.merge` 消费。所以重复节点不会产生。

但"必须停掉抓取任务"这个结论依然对，只是理由要换：

| 真实伤害 | 机制 |
|---|---|
| 状态被打回 | `kg/fetch.py:334` 无条件 `_set_request_status(..., 'pending_review')`，已 `approved` 的变回待审 |
| 白花一次 USDA 额度 | 每日预算 50（`config.py:321`），花在一条注定被否决的任务上 |
| 多一条废 bundle | 带 `hard_veto` 的 bundle 仍会落库，得人去补录审核页手动驳回 |

**(2) 行号小幅偏差**：`all_aliases` 在 `kg/normalize.py:125` 不是 `:252`；`canonical_name` 在 `:83` 不是 `:81-82`。不影响结论。

**(3)「换字同义写死一张表」——可行，但扩展性差。**
猪脚/猪蹄、牛油果/鳄梨、番茄/西红柿只是冰山一角。更划算的顺序：先跑一遍 `scripts/load_crosswalk.py` 那条链路看已有关联表能覆盖多少，剩下的按"提问热度 Top 50"人工确认，最后才轮到手写。成本相当，覆盖面宽得多。

### 9.3 评审漏掉、我补的两条

**(1) §三 P2 的判断本身是错的。**
`api/admin.py:355` 的 SELECT 里**已经带了 `normalized`**，只是 `Requests.vue:15-16` 只渲染了 `food_key` 和 `raw_mention`。所以不是"归一化没生效"，是"归一结果查出来了但前端没显示"。修法是前端加一列/合并展示，**不需要动 `normalize_food_term`**。这一条把 P2 从"抽取层问题"降级为"展示层问题"，工作量从 P2 级掉到 P8 级。

**(2) 行号持续漂移，是元层面的风险。**
`config.py` 348→355、`uncatalogued_node` `:114`→`:156`，两次读数都不同。本文有 40 多处 `文件:行号`，开工时大半会失效。**建议 v5.1 起只引函数名/符号名**，行号统一标注"以开工时为准"，别再维护行号。

### 9.4 与 v4 的硬冲突（必须等 v4 落地才能动）

`docs/plan/plan_v4.md` 已被修订（现 442 行，新增 §1.2 切词实测表），它的 M1 与 §5.2 明确要给 `_BREAK_WORDS` 补「煲汤」「煲」：

| 计划 | 改 `_BREAK_WORDS` 的哪一批词 | 位置 |
|---|---|---|
| v4 | 煲汤、煲 | `plan_v4.md:257`，附 §1.2 的副作用表（「煲仔饭」→「仔饭」） |
| v5 | 每天、每日、一天、每周、早上、晚上、空腹、饭前、饭后、睡前、适量、营养价值、营养成分表、热量表、能不能吃 | 本文 §五 5.2 |

**同一张表、两个计划、两批词。** 合并规则：以 v4 先落地的词为准，v5 只做**追加**（`|` 级 union），不删不改 v4 的词；合并后必须重跑 `tests/test_food_request.py` 与 `tests/test_retrieval_plan.py`，并人工核一遍 v4 §1.2 表里的 `猪脚/天麻` 切词结果没有被 v5 的词带偏。

### 9.5 本文勘误表（正文不改，以此表为准）

| # | 正文位置 | 现文（错） | 正确 |
|---|---|---|---|
| 1 | §零 摘要 | 合计 4.5～5 人日 | **6 人日**（M3 前端 1.5 → 2.5，营养素表单要自建，不能复用 `Import.vue`） |
| 2 | §三 P5 表 | 牛油果/鳄梨 距离 2 → 0.33 | 距离 **3** → **0.00** |
| 3 | §三 P5 表 | （缺） | 补一行：羽衣甘蓝/甘蓝 包含关系 → **0.50** |
| 4 | §三 P5 表 | `config.py:285` | `config.py:292`（现文件 355 行） |
| 5 | §三 P2 | 「归一化对这批数据完全没生效」 | 归一有值（`api/admin.py:355` 已 SELECT `normalized`），是前端 `Requests.vue:15-16` 没用 —— **展示层问题** |
| 6 | §三 P7 | `config.py:297` | `config.py:304` |
| 7 | §四 D4 L2 | `MATCH (n:Ingredient) WHERE n.name CONTAINS $kw` | `MATCH (n:Ingredient) WHERE n.canonical_name = $name`（**精确同名**，专补 P6 的洞）；换字同义改走同义表，不走 CONTAINS |
| 8 | §四 D5 | `_set_request_status(..., 'rejected'\|'queued')` | 按可重试性分四出口，见 §10.2 表 |
| 9 | §五 5.2 | `_NON_FOOD_TERMS` 判定改为**子串包含** | 保持**整词相等**；新词整词加进集合，切分交给 `_BREAK_WORDS` |
| 10 | §五 5.2 | `run_fetch.py` 增 `--request-id` | 已有 `--task-id`；页面按 `request_id` 查出 `task_id` 复用现成命令，接口内不跑抓取 |
| 11 | §五 5.3 | `"canonical_id": "Ingredient:123"` | `"canonical_id": "ING_00123"` |
| 12 | §五 5.4 | `migrations/020_request_triage.sql` | `migrations/021_request_triage.sql`（020 被 `020_turns.sql` 占用） |
| 13 | §五 5.3 / 5.5 | 批量 `action ∈ reject\|alias\|ignore_noise`；线框有「批量挂别名」 | 只留 `reject` / `ignore_noise`（与 §七 R4 一致） |
| 14 | §六 断言 1 | `match("猪脚")` 带出「猪蹄」 | 改对着同义表断言：返回 `likely_exists` + `ING_00xxx`（猪蹄），`layer="synonym_table"`；模型（M6）不进这条断言 |
| 15 | §六 断言 5 | 唯一索引会挡住重复 bundle | 拆成两句：① 同来源 `manual_entry` 再提交 → 更新原 bundle；② 已有 `usda_fdc` 未结 bundle → 返回 **409** 并带 bundle id |
| 16 | §六 断言 8 | `extract_candidates("章鱼营养价值") == []` | **`["章鱼"]`**（切分后留下食物名）；`["奇亚籽"]` |
| 17 | §六 测试命令 | 含明文口令的 `PG_DSN` | 用环境变量指向 `nutri_graph_test`，文档不写口令 |
| 18 | 全文 | 40+ 处 `文件:行号` | 行号以开工时为准；v5.1 起改按符号名引用 |
| 19 | §四 D6 | `FOOD_FETCH_DAILY_BUDGET=50`，`config.py:314` | 值对，行号是 **`:321`**（本次共 3 处 config 行号全偏 7 行，规律是文件涨了 7 行） |

### 9.6 评审完全同意的部分（不用改）

先归类再动作、不允许一键补录、admin 不写 `MERGE`、补录复用 `importer.validate_row`、模型只出候选、不做自动挂别名、OCR/C 端上报/成分表导入留到 v6、抓取默认人触发、审计表单独建（`reject_reason` 表达不了"挂到了哪个 `ING_`"）、噪音清理先 `--dry-run`。这些在 §四 与 §七 里已经写对，评审也认可，重启时照旧。

---

## 十、冻结与重启条件

### 10.1 前置条件（全部满足才开工）

- [ ] **P0 `plan_v4.md` 全部落地**：M1/M2 上线、`WEB_SEARCH_ENABLED` 已按灰度打开并观察过 `web_search_log`
- [ ] **P0 v4 的 `_BREAK_WORDS` 改动已合并并回归通过**（见 §9.4）；v5 只追加不覆盖
- [ ] `020_turns.sql` 已在生产跑过，确认它没有遗留给 v5 依赖的表
- [ ] 重新核对本文所有符号的当前行号（**只核符号，别抄旧行号**）
- [ ] 工时按 §9.5 第 1 条复审（5.0 → **6.0** 人日），确认排期
- [ ] 断言 1 / 5 / 8 / 9 已按 §9.5 重写（这四条现在都不可执行）

### 10.2 D5 四条出口的正确形态（替换正文）

| 出口 | 任务 | request |
|---|---|---|
| `process_task` 画像失败、未捕获异常 | `failed` | `rejected` |
| 无匹配但 `attempts < MAX_ATTEMPTS` | `queued` + `next_retry_at` | **维持 `queued`** |
| 同上，次数已用尽（`MAX_ATTEMPTS=3`） | `failed` | `rejected` |
| 成功 | `done` | 维持今天的 `pending_review` / `approved` |

关键点：**`queued` 只能表示"还可以再来一次"**，不能既表示"排队中"又表示"已死"。可重试的失败若打成 `rejected`，因为 `register()` 的 `ON CONFLICT` 不恢复状态，这条食物就永久失去抓取机会。

### 10.3 重启后的执行顺序（对 review §5 的微调）

```
迁移 021
  → M1 反查（L2 改成精确同名；同义表先查 crosswalk 存量）
  → M2 三个动作（alias 走 sync_aliases + 409 + refresh + 同事务停任务；bundle 写回 request_id）
  → M4 四条出口 + 抓取按钮（复用 --task-id）
  → M5 词表合并（在 v4 之后，只追加）+ 存量清理 --dry-run
  → M3 页面（自建营养素表单，2.5 人日）
  → M6 模型同义（默认关，不挡前面任何一条，不进断言）
```

### 10.4 冻结期间允许 / 不允许

| 允许 | 不允许 |
|---|---|
| 只读核对代码（发现行号漂移就记进本文件） | 改任何代码 |
| 补充本文的勘误与风险 | 建迁移、改词表 |
| 在 v4 期间顺手记录"v5 会用到的实测数据" | 提前动 `kg/request.py` / `api/admin.py` |

> 冻结解除的唯一信号：**v4 的 M1–M3 全部完成且线上观察期结束**。解除时本文升版为 v5.1，按 §9.5 逐条订正后再视为可执行计划。

---

## 十一、v4 落地后的可行性复审（2026-10-29 13:00）

### 11.1 结论：**可行，但不是照原样执行**

v4 落地后，v5 依赖的 8 个模块里有 5 个**完全没动**，诊断与方案的主干全部成立。需要先订正 6 处、补 2 项，工时从 6.0 微调到 **5.5 人日**。

§10.1 的前两项前置条件已经满足：v4 已实现（`kg/webanswer.py` / `kg/webguard.py` / `kg/websearch.py` / `migrations/021_web_answer.sql` 均在），`_BREAK_WORDS` 已按 v4 §1.2 补了「煲汤」「煲」，§9.4 的词表冲突**自动解除**。

### 11.2 依赖面体检

| v5 依赖的模块 | v4 是否改 | 事实 | 对 v5 的影响 |
|---|---|---|---|
| `admin-web/src/views/Requests.vue` | ❌ 没动 | 全文仍是原样：无操作列（`:14-24`）、抽屉只有驳回 | **P1 / P2 / P8 全部成立**，方案不用改 |
| `api/admin.py` | ❌ v4 没动 | v3.1 只给 candidates 加了敏感词扫描；`/kg/requests` 系列逻辑一字未改，但整体下移 **+19 行**（`list_food_requests` 328 → **347**） | 方案成立，行号作废 |
| `kg/request.py` | ✅ 改了 2 处 | ① `_BREAK_WORDS` 加「煲汤」「煲」 ② `register()` 话术改成「暂未收录「X」。已登记（编号 #N）。」 | P3 / P5 / P6 分析不变；全体行号 **+1**（`register` 187、`exact_lookup` 231、`fuzzy_lookup` 256） |
| `kg/fetch.py` | ❌ v4 没动 | v3.1 让 bundle 带上 `sensitive_hits`（+10 行），**失败出口逻辑一字未改** | **P4 成立**：`_set_request_status` 仍只有成功路径一处；失败出口现为 **304 / 314 / 333 / 339**，成功 **344** |
| `kg/import_bundle.py` | ❌ 没动 | `_stage_one` 仍是 `VALUES (NULL, ...)`（**:178**），`request_id` 恒空 | D3 的修法不变 |
| `kg/promote.py` | ❌ v4 没动 | v3.1 只改 bundle 详情的敏感词展示；`sync_aliases`（**:546**）全文**没有** sensitive 扫描 | 见 §11.4(1)，这是**新缺口** |
| `kg/textsim.py` / `kg/schema.py` / `kg/normalize.py` | ❌ 没动 | 0.50 / 0.00 的结论、`canonical_name`、`all_aliases(refresh=)`（`:125`）全部原样 | P5 / P6 成立 |
| `graph/nodes.py` | ✅ 大改 | 拆出 `_register_unresolved` / `_eligible_terms` / `_visible_requests` / `_food_payload`，新增 `web_answer_node` | v5 **不改这个文件**，但要接受两个行为变化（§11.5） |
| `migrations/` | ✅ 新增 2 个 | `021_web_answer.sql`、`022_sensitive_word_seed2.sql` 已占号 | **v5 的迁移必须用 023** |

### 11.3 六处订正（覆盖 §9.5 对应行）

| # | 项 | §9.5 写的 | 现值（开工按这个） |
|---|---|---|---|
| 1 | 迁移号 | 021 | **023**（021 web_answer、022 sensitive_word_seed2 已占） |
| 2 | `config.py` 阈值 | 292 | **293**（0.62） |
| 3 | `config.py` 手工限流 | 304 | **305**（40 条/天） |
| 4 | `config.py` 抓取预算 | 321 | **322**（50/天） |
| 5 | `kg/request.py` 各符号 | 51 / 64 / 71 / 117 / 142 / 187 | **51 / 64 / 71 / 118 / 143 / 187**（`_BREAK_WORDS` 现在是 51-61，含煲汤/煲） |
| 6 | `api/admin.py` 四个接口 | 328 / 365 / 385 / 405 / 446 | **347 / 384 / 404 / 424 / 465** |

> 规律：v3.1 让 admin.py +19、fetch.py +10；v4 让 request.py +1、config.py +1。**别抄行号，按符号名找**（§9.3(2) 的判断被再次验证）。

### 11.4 v5 原计划没有、现在必须补的两项

**(1) `POST /alias` 必须过敏感词闸 —— 这是 v4/v3.1 之后才存在的缺口。**

v3.1 把敏感词闸铺到了：`register()`（`kg/request.py`）、`importer.validate_row`（`kg/importer.py:723-728`）、`promote.approve_bundle`（`kg/promote.py:270 _sensitive_gate`）、`api/admin._set_status`（candidate 批准口）。

**唯独 `sync_aliases` 没有。** 而 v5 的 `/alias` 正是往这里写，写完立刻生效于 `graph/nodes.py:97 link_node` —— 等于在敏感词防线上开了一个后门：把一个政治/疗效词挂成「鸡腿」的别名，之后用户输入该词会被链到鸡腿节点上，绕过输入侧拦截的语义。

做法：调 `sync_aliases` 之前 `scan(alias, field_name="alias")`，`block` 级直接 409 不写，`review` 级要求 `confirm_sensitive`（与 `_set_status` 同款语义，前端要能看见命中词）。

**(2) 「停掉抓取任务」不用自己写，抄 `reject_bundle` 的现成写法。**

`kg/promote.py:530-539` 已经有正确形态，而且注释把理由写清楚了（"负缓存：任务置 rejected 后 `_claim` 不会再捡它，不会天天重抓同一个错误词"）：

```sql
UPDATE kg_ingredient_request SET status = 'rejected' WHERE id = %s;
UPDATE kg_fetch_task SET status = 'rejected' WHERE request_id = %s;
```

v5 的 `/alias`、`/reject`、`batch(ignore_noise)` 三个动作都要带上这两条，且与主动作**同事务**。

> 顺带确认一个好消息：**补录不用额外做敏感词闸**。`POST /bundle` 复用 `importer.validate_row`，而它在 `kg/importer.py:723-728` 已经 `scan_obj(payload, IMPORT_FIELDS)` 并把命中写进 `payload["_sensitive"]`。所以缺口只有 `/alias` 一处。

### 11.5 两个行为面变化（影响文案与验证）

1. **`_eligible_terms` 会丢掉自述词、且一句话最多登记 2 个**（`graph/nodes.py` 的 `_SELF_REPORT`）。所以「我怀孕了，猪脚和天麻能煲汤吗」这类不会再往队列里塞「我怀孕了」。v5 的存量清理脚本跑下去，**自述类噪音应该已经是 0 条**；如果还有，说明是 v3.1 之前的旧数据。
2. **`register()` 的话术变了**：从"审核通过后就能查到…在那之前我不会给出营养数据"改成"暂未收录「X」。已登记（编号 #N）。"。后台「手工登记」的返回 `message` 直接透传这句，v5 的抽屉文案不要再引用旧话术。

> 队列噪音结构因此变成：自述类已消失，**「章鱼营养价值」「奇亚籽每天」这类仍然全量入库**（v4 没碰 `_BREAK_WORDS` 里的营养/频次词）。**P3 仍然必须做**，只是存量比截图时干净一些。

### 11.6 工时复核

| 阶段 | 内容 | 原估 | 现估 | 变化原因 |
|---|---|---|---|---|
| M1 反查 | `kg/lookup.py` + `/matches` + 迁移 | 1.0 | 1.0 | — |
| M2 动作 | `/alias`、`/bundle`、`/batch` + 审计表 | 1.0 | **1.25** | 新增敏感词闸 + 停任务同事务 |
| M3 前端 | 表格/抽屉/补录表单 | 2.5 | 2.5 | 视图层未受影响 |
| M4 状态机 | 四出口 + 抓取按钮 | 0.5 | 0.5 | 出口位置变了但数量没变 |
| M5 词表 | 追加词 + 存量清理 | 0.5 | **0.25** | 冲突已解，只需追加；自述类噪音已消失 |
| M6 模型同义 | `kg/synonym.py`，默认关 | 0.5 | 0.5 | 不挡任何验收 |
| — | **合计** | 6.0 | **5.5** | |

### 11.7 一句产品判断

v4 之后 C 端「未收录」已经有了临时答案，**运营补库的紧迫感会下降**——这是 v5 真正要说服人的地方。理由只有一条：v4 的答案是**不落库、不可复用**的，缓存 7 天到期后同一个问题还要再搜一次，我方每次都付搜索成本，而图谱覆盖率**永远不动**。v5 不做的代价不是"页面难看"，是"同一笔钱反复花"。

### 11.8 开工顺序（替换 §10.3）

```
迁移 023（triage 字段 + kg_request_action）
  → M1  L2 改精确同名（n.canonical_name = $name），同义表先查 crosswalk 存量
  → M2  /alias（scan → sync_aliases → 409 → refresh → 同事务停任务 → 审计）
        /bundle（name 锁 food_key、写回 request_id、已有 usda_fdc 未结则 409）
        /batch（只留 reject / ignore_noise，同样停任务）
  → M4  四条出口 + 抓取按钮（复用 run_fetch --task-id，接口内不跑抓取）
  → M5  只追加词表 + 存量清理 --dry-run（自述类已为 0，不要再写清洗逻辑）
  → M3  页面（选择列/操作列/排序/抽屉疑似匹配/自建营养素表单）
  → M6  模型同义，默认关，不进任何断言
```

---

## 十二、第二轮评审复核（2026-10-29 13:20）

> 对象：`docs/review/review_v5.md`（第二轮，112 行，13:13 重写）
> **结论：五处新意见全部成立，逐条已在代码里验到。其中两处是我自己写错的，一处是我上一轮的"订正"本身就是错的。**

### 12.1 我错在哪

| 我的错误 | 位置 | 事实 |
|---|---|---|
| 让 `/alias` 抄 `reject_bundle` 的两条 SQL | §11.4(2) | 第一条会把登记打成 `rejected`，与"挂上别名即已收录"直接矛盾。**只该停任务，不该驳回登记** |
| 说两列一样"是展示层问题，把 `normalized` 显示出来就行" | §9.3(1) | **错了两次**：`register()` 的插入值是 `food_key = term`、`normalized = term`（`kg/request.py:326`），两列**永远相同**；就算前端画出来，截图仍是「天麻 / 天麻」 |
| 开工表里 `normalize_food_term` / `extract_candidates` 写成 118 / 143 | §11.3 第 5 行 | 实际 **117 / 142**，我抄的是改行前的旧值 |

### 12.2 五处新意见：逐条验到

| 评审意见 | 我的核实 | 采不采纳 |
|---|---|---|
| `/alias` 若执行"改 rejected"会自相矛盾 | `kg/promote.py:531-538` 确实同时改 request 与 task；`_CLAIM_SQL`（`kg/fetch.py:92-96`）只领 `status='queued'`，所以停任务只需改 task | ✅ 采纳，见 §12.3 |
| `field_name="alias"` 扫不到疗效词 | `NAME_FIELDS = ("name","aliases","english_name","scientific_name")`（`kg/sensitive.py:31`），`_is_name_field` 只多认 `canonical_name`（`:190`）；`_effective_action` 对非名称字段 + `name_only` 直接返回 **None**（`:201-202`）→ 命中被丢弃 | ✅ 采纳，改传 `"aliases"` |
| 两个函数各自提交，"同一事务"做不到 | `db.connect()` 文档串写明"**正常退出提交**，异常回滚"（`db/engine.py:112-114`）；`sync_aliases` / `stage_bundles` 都自带 `with db.connect()` | ✅ 采纳，连接由调用方传入 |
| 别指望 crosswalk 产生断言 1 | `data/crosswalk/ingredient_canonical.csv` 共 51 行；有 `西红柿→番茄`（第 2 行）、有 `猪肝`（第 27 行），**没有猪脚/猪蹄/牛油果/鳄梨/羽衣甘蓝** | ✅ 采纳，同义表必须单独落地 |
| 断言 3 与"长度 > 4 就丢弃"没划掉 | §10.1 只点名重写断言 1/5/8/9；正文断言 3 仍写"子串规则返回 `not_food`"，§5.2 仍写"长度 > 4 且含非食物词就丢弃" | ✅ 采纳，一起重写 |
| `sync_aliases` 会先把候选置 approved 再返回 skipped | `kg/promote.py:581-587` 的 `UPDATE dict_alias_candidate SET status='approved' ... WHERE alias_norm = ANY(%s)` 用的是**整个 `norm_map`**，包含刚被跳过的词 | ✅ 采纳（**这是评审最狠的一条**，不改会污染候选表） |

### 12.3 三个动作的正确形态（权威，覆盖 §11.4 的表）

| 动作 | `kg_ingredient_request` | `kg_fetch_task` | `dict_alias` | 失败/边界 |
|---|---|---|---|---|
| **挂别名成功** | `approved` | `rejected` | 写入 | 别名被占用 → **409**，登记保持原状，候选状态不动 |
| **驳回 / 判噪音** | `rejected` + 理由枚举 | `rejected` | 不动 | — |
| **补录提交成功** | `pending_review` | `rejected` | 合并时才写（不动） | 已有 `usda_fdc` 未结 bundle → **409**（提交后立刻停任务，否则 `:344` 仍会回写） |
| **立即抓取** | 维持 `queued` | 终态先放回 `queued` 再 `--task-id` | — | **已挂别名的行不给这个按钮**，否则 `:344` 把 `approved` 打回 `pending_review` |

> 记住一条：**`rejected` 只属于"这条登记不成立"，`rejected` 的 task 只属于"不用再抓了"。两者不能混用。**

### 12.4 我补的三点（评审没提，但会影响能不能落地）

**(1) 同义表要落成数据文件，不能只做测试夹具。**

评审说"断言 1 用一张写死的同义表"。如果这张表只存在于 `tests/`，断言 1 就是**自证**：测试自己塞了「猪脚→猪蹄」，生产路径根本没有这条映射，跑绿了什么也没证明。

做法：`data/synonyms/ingredient_synonyms.csv`（`alias,canonical_name,note`），由 `kg/lookup.py` 在 L3 之后读入（进程内缓存，几十条，启动时加载）；运营在抽屉里确认一条同义关系时**写回这个文件或一张表**，形成积累。断言 1 断言的是"这条映射被系统读到了"，不是"我在测试里造了一条"。

**(2) `sync_aliases` 加连接参数必须向后兼容。**

它有两个调用方：`kg/promote.py` 内部（合并路径）+ v5 的 `/alias`。签名改成 `sync_aliases(..., conn=None)`，`None` 时自己开连接，**不要改成必传**——否则合并路径（`tests/test_promote.py` 覆盖着）整片回归，而那块是打过 120 个节点事故的区域，不值得为省一条连接去碰。

**(3) 工时回到 6.0。**

§11.6 的 5.5 低估了第二轮的改造量：连接改造（两个函数）+ 候选状态修复 + 同义表数据化 + 断言 3 重写。M2 从 1.25 → **1.75**，合计 **6.0**。

### 12.5 开工清单（替换 §11.8）

```
0  先做三件"不改功能只改口径"的事
   a. 从本文 §六 删掉带口令的 pytest 命令（勘误 17 至今没落到正文）
   b. 断言 3 重写：队列里的旧键「章鱼营养价值」由清理脚本驳回；
      match() 只在**整词命中** _NON_FOOD_TERMS 时返回 not_food
   c. 删掉 §5.2 的「长度 > 4 且含非食物词就丢弃」（与切分重复，且会按子串误杀）

1  迁移 023：triage / triage_hit_id / triage_at / handled_* + kg_request_action
   + dict_alias_candidate 的状态不被 sync_aliases 误改（逻辑改，不需要列）

2  M1 反查  kg/lookup.py
   L1 exact_lookup → L2 n.canonical_name = $name（精确）→ L3 0.62 不变
   → L3.5 同义表 data/synonyms/ingredient_synonyms.csv
   → L4 模型（默认关，不进断言）
   前置：match() 先判整词 _NON_FOOD_TERMS → not_food
   写入：GET /matches 负责写 triage / triage_hit_id / triage_at，列表接口只读

3  M2 三动作（全部带 conn，见 §12.3 表）
   /alias   scan(alias, field_name="aliases") → block 409
            → sync_aliases(conn=...) → 候选状态不动 → all_aliases(refresh=True)
            → request=approved, task=rejected（同事务）
   /bundle  validate_row → stage_bundles(conn=..., request_id=...) 插入时写入
            → request=pending_review, task=rejected；已有 usda_fdc 未结则 409
   /batch   只留 reject / ignore_noise，同样停任务

4  M4 四条出口（§10.2）+ 抓取按钮（只拉起 run_fetch --task-id）

5  M5 词表只追加 + 存量清理 --dry-run（自述类噪音已为 0，别写那块清洗）

6  M3 页面：选择列 / 操作列 / 按次数排序 / 抽屉疑似匹配 / 自建营养素表单
   顺带定「归一名」怎么显示：两列在 asis 情况下必然相同，
   改成**一列**「原话」（等同名则不重复显示），非同名的显示「黑豆粥 → 黑豆」

7  M6 模型同义，默认关
```

---

## 十三、定稿施工口径（v5.3，取代 §11.4 / §11.8 / §12.4 / §12.5）

> 第三轮评审（`docs/review/review_v5.md`，76 行，13:30）复核结论：**4 条全部成立**。
> 本节把三轮结论收敛成一份可直接照着写的口径 —— 前面几节只作过程记录，冲突时以本节为准。

### 13.1 第三轮我错的两处

| 我的写法 | 位置 | 问题 |
|---|---|---|
| `/alias` 成功后「候选状态不动」 | §12.5 第 3 步 | 把「409 时不动」误写成「成功时也不动」。照这句实现会删掉 `sync_aliases` 更新候选的语句 → 挂过的别名永远留在「疑似别名」待审列表里，断言 12 也过不了 |
| 同义确认「写回 `data/synonyms/` 文件，形成积累」 | §12.4(1) | 文件在代码目录里，发布会盖掉运营补的行；进程内缓存要重启才生效。**确认只能写 `dict_alias`，种子文件只读** |

另有两处过期文本：文首冻结框第 3、4 条（已于本次改掉）、§六 带口令的测试命令（仍未删）。

### 13.2 第三轮四条：逐条验到

| 评审意见 | 核实 | 采纳 |
|---|---|---|
| 成功挂上的别名，候选行要改 `approved` | `kg/promote.py:581-587` 的 `WHERE alias_norm = ANY(%s)` 用的是整个 `norm_map`，把被跳过的词也置了 approved。修法不是"整段去掉"，是**收窄到本次真正写入的 `alias_norm`**：成功的那条变 approved，被占用时写入列表为空、自然不动 | ✅ |
| 409 必须在连接块内抛 | `db.connect()` 块内异常会 `conn.rollback()`（`db/engine.py:131-138`）。先退出 `with`（已提交）再返回 409，候选行已经落库 | ✅ |
| 种子文件只读，确认只写 `dict_alias` | 代码目录 + 单 worker 进程内缓存 → 运行时写文件既会被发布覆盖，也不会立刻生效 | ✅ |
| 名称字段没有 `review` 档，不用 `confirm_sensitive` | `_effective_action`（`kg/sensitive.py:193-204`）：名称字段一律返回 `block`；只有非名称字段才可能 `return default`。`scan()` 用它折算（`:220`） | ✅ 简化设计 |
| 羽衣甘蓝/甘蓝不能进同义表 | 羽衣甘蓝 = kale，甘蓝 = cabbage，是两种菜。0.50 的相似度是**该被判为"不像"的证据**，不是"像"的线索 —— 挂上去就是 §七 R1 要防的错别名 | ✅ 从我 §9.5 勘误第 3 条的措辞里也去掉"可作同义线索"的暗示 |
| 番茄/西红柿不必再写一份 | `data/crosswalk/ingredient_canonical.csv:2` 已有 `ING_00001,西红柿,...,番茄;洋柿子;tomato;圣女果` | ✅ |

### 13.3 `/alias` 的正确形状（代码级）

```python
# POST /admin/kg/requests/{id}/alias   body {canonical_id, alias}

with db.connect() as conn:                       # ← 409 必须抛在这个块内，否则已提交
    hits = scan(alias, field_name="aliases")     # 名称字段 → 一律 block，无 review 档
    if any(h.action == "block" for h in hits):
        log_hits("alias", hits, source="admin")
        raise HTTPException(409, {"code": "sensitive_blocked", "message": ...})

    r = sync_aliases(canonical_id, [alias], source="admin_request",
                     confidence=1.0, conn=conn)  # conn=None 时自开，向后兼容
    if not r["written"]:                          # 被别家占用 → 什么都没写
        raise HTTPException(409, {"code": "alias_occupied", **r["skipped"][0]})

    conn.execute("UPDATE kg_ingredient_request "
                 "SET status='approved', handled_by=%s, handled_at=now() WHERE id=%s", ...)
    conn.execute("UPDATE kg_fetch_task SET status='rejected' WHERE request_id=%s", ...)
    conn.execute("INSERT INTO kg_request_action (request_id, action, payload, actor) "
                 "VALUES (%s,'alias',%s,%s)", ...)

get_alias_store().all_aliases(refresh=True)       # ← 提交之后才刷，否则缓存指向未提交的行
```

`sync_aliases` 内部同步改一处（对应 §13.2 第 1 条）：

```python
# 原来：WHERE alias_norm = ANY(list(norm_map))   ← 含被跳过的词
# 改为：只更新这一次真正写进去的（被占用时列表为空，天然不动）
if written:
    conn.execute("UPDATE dict_alias_candidate SET status='approved', suggested_canonical_id=%s "
                 "WHERE entity_type='Ingredient' AND alias_norm = ANY(%s)",
                 (canonical_id, [normalize_text(w) for w in written]))
```

### 13.4 同义种子文件规格

| 项 | 定 |
|---|---|
| 路径 | `data/synonyms/ingredient_synonyms.csv` |
| 列 | `alias,canonical_name,note` |
| 本期成员 | `猪脚,猪蹄`、`牛油果,鳄梨` |
| **不放** | 羽衣甘蓝/甘蓝（两种菜）；番茄/西红柿（crosswalk 第 2 行已有） |
| 读写 | **只读**。服务启动时载入进程内缓存（`--workers 1`，发布重启自然生效） |
| 确认一条同义时写哪 | **只写 `dict_alias`**（`sync_aliases` + `all_aliases(refresh=True)`）。种子文件不动 |
| 积累 | 另建表（v6），本期不做；本期种子随代码发布 |
| 解析成 `ING_` 的顺序 | ① `exact_lookup(canonical_name, entity_type="Ingredient")` ② Neo4j `MATCH (n:Ingredient) WHERE n.canonical_name = $name` ③ 都没有 → 该命中**不带 id**，抽屉不显示「确认挂别名」 |

> 为什么种子值只有"别名 → 规范名"而没有 id：种子是**人写**的，写的时候不该知道 `ING_` 编号；id 是运行时解析出来的。这也正好补上 P6 那个"图谱有节点、别名表没行"的洞 —— 第 ② 步就是干这个的。

### 13.5 定稿清单（唯一施工顺序）

```
0  口径（不碰业务逻辑）
   a. 删掉 §六 里带口令的 pytest 命令
   b. 断言 3 重写：match() 只在**整词**命中 _NON_FOOD_TERMS 时返回 not_food；
      队列里的旧键「章鱼营养价值」由清理脚本驳回，不由 match 判
   c. 删掉 §5.2 的「长度 > 4 且含非食物词就丢弃」（与切分重复，且会按子串误杀）
   d. 断言 1 改为「同义种子 + 三步解析」；羽衣甘蓝/甘蓝不进种子

1  迁移 023
   kg_ingredient_request: triage / triage_hit_id / triage_at / handled_by / handled_at
   + kg_request_action(request_id, action, payload, actor, created_at)
   同时改代码：sync_aliases 的候选状态更新范围收窄到 written（§13.3）

2  M1  kg/lookup.py
   L1 exact_lookup → L2 canonical_name 精确 → L3 0.62 不变
   → L3.5 同义种子（三步解析成 ING_）→ L4 模型默认关、不进断言
   match() 先判整词非食物；GET /matches 写 triage 三列，列表接口只读

3  M2  三动作（§13.3 形状 + §12.3 状态表）
   /alias   → request=approved, task=rejected
   /bundle  → validate_row → stage_bundles(conn=..., request_id=...) 插入时写入
              → request=pending_review, task=rejected；已有 usda_fdc 未结则 409
   /batch   → 只留 reject / ignore_noise，双双 rejected
   已挂别名的行不给「立即抓取」按钮（否则 :344 把 approved 打回 pending_review）

4  M4  四条出口按 §10.2；抓取按钮只拉起 run_fetch --task-id，请求内不跑 process_task

5  M5  词表只追加（v4 已加煲汤/煲，别删）；存量清理脚本先 --dry-run，自述类噪音已为 0

6  M3  页面：选择列 / 操作列 / 按次数排序 / 抽屉疑似匹配 / 自建营养素表单
       「原话」与「归一名」合并成一列：同名只显示一个，异名显示「黑豆粥 → 黑豆」

7  M6  kg/synonym.py，模型同义，默认关
```

**工时**：6.0 人日（M2 含连接改造与候选状态修复，1.75）。

### 13.6 收敛声明

三轮评审的边际收益已经明显下降 —— 第三轮只剩两处措辞/设计错和两处过期文本，没有再出现"照做会写错代码"级别的问题。

**本文到此冻结为 v5.3，不再接第四轮文字评审。** 开工后如遇新问题，记到单独的实现笔记里（建议 `docs/plan/v5_impl_notes.md`），不再回头改这份计划 —— 否则计划会一直在改、代码一行没写。
