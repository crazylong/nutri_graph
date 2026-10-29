# v3 代码评审

> 对照 `docs/plan/plan_v3.md`（含第六点五的实施记录）审当前工作区。五条需求都已落地，这次看的是实现和步骤是否对得上。
> 日期：2026-10-29。

## 1. 结论

主干是对的：录入按字段分级，政治类问句打码落库，疗效问句不整句拒答，三个批准口都调用了 `assert_approvable`，连接池显式提交，多轮按轮保留最近 3 轮并三段读取。上一轮连接池评审里要改的项已经在代码里。

还不能按「v3 已完成」收口。正常使用会踩到四件事：

1. 备注里的 review 级命中，审核详情看不见，确认框也不出现，批准会一直 409。
2. 关系候选的批准接口不接收 `confirm_sensitive`，页面上的 review 级命中同样批不出去。
3. 输出扫描把「最好 / 最佳」这类句子一起删掉；删光之后换成疗效兜底话术。
4. 红旗问句里夹带的政治词会进 Langfuse 的 input。消息表打了码，trace 没有。

合并进图时，拒绝日志会写下 `claim_text` 的前 40 字，和「日志不留原词」冲突。

## 2. 和计划对齐的部分

输入侧只扫 `political`。`sensitive_guard` 挂在 `guard` 之后、`normalize` 之前，命中边接到 `memory_write`。`_redact` 写在 `RAGState` 里。用户侧和会话标题存 `[内容已过滤]`，助手侧保留固定话术或就医回答，画像抽取在打码时跳过。`register()` 对名称上的 block 拒绝写入，`uncatalogued_node` 把这类结果从 `gaps` 和 `_food_requests` 里摘掉。

导入行在 `validate_row` 里按名称 / 描述折算动作。名称上的疗效词是 error，描述里的「清热解暑，有健脾功效」可以通过，描述里的「加微信」是 warn 且仍可提交。`approve_bundle` 重扫后，block 拒绝，review 要求 `confirm_sensitive`。CLI `kg.review approve` 有 `--confirm-sensitive`。输出扫描对政治类整段替换，对疗效句按句号切掉并保留前后事实句；免责声明不参与扫描。`CONTENT_MODERATION_PROVIDER` 只有常量和注释。

连接池是 `raw_connection()` 加显式 `commit()`，池按 DSN 加锁缓存，`connect_timeout=3`，提交或回滚失败会 `invalidate()`。`_load_profile` 遇到 `PoolBusy` 原样抛出。systemd 的 `ExecStart` 写了 `--limit-concurrency 10`，`python app.py` 也会带上这个上限。`api/health.py` 仍是短超时直连。

`save_round` 在事务里对 session 加锁，一轮两条消息共用一个 `turn_no`。红旗短路改走 `memory_write`。`load_context` 在摘要还没写完时仍带上更早轮次的原文。摘要写回把旧摘要交给模型。`GET /sessions` 返回 `summary` 和 `round_count`，侧栏不展示摘要。会话内过滤只匹配已加载的 user 消息，侧栏有「本轮问过」目录，`?q=` 先过 `owns()`，没有 pg_trgm 索引。

## 3. 要改

### 3.1 备注上的 review 命中，详情页看不见，也确认不了

计划里争议档（描述里的疗效词、营销词）要进审核队列，审核员看过再勾确认。后端闸做了，页面用的是另一份扫不到备注的结果。

`kg/import_bundle.py` 落库时用 payload 扫，payload 上有 `note`，所以 `sensitive_hits` 里有备注命中，列表上的「待确认」计数是对的。`kg/promote.py` 的 `get_bundle` 在读出 items 之前，对 bundle 行做实时重扫。备注不在 `kg_food_bundle` 的列上，它在 `kg_food_bundle_item` 里。实时结果因此是空数组。

`admin-web/src/views/BundleDetail.vue` 用 `sensitive_hits_live || sensitive_hits`。空数组在 JavaScript 里是真值，落库那份命中被丢掉。确认框依赖这份空列表，不会出现。提交时 `confirm_sensitive` 仍是 false。`approve_bundle` 会从 item 里读到备注并返回 409，文案要求带 `confirm_sensitive=true`。审核员在页面上没有地方把这个标志设为 true。

一条备注写着「有助于治疗」的导入行，列表上能看到待确认，点进去没有命中、没有勾选，点通过被拒绝。这正是计划要灰度观察的那一档。

实时重扫要带上 items 里的 `note`（以及下面 4.1 的 `english_name`）。页面在实时结果为空时继续用落库的 `sensitive_hits`，或者后端不要用空列表表示「没扫到备注」。

### 3.2 关系候选的批准口收不了确认

`api/admin.py` 的 `_set_status` 已有 `confirm_sensitive`，三个批准口里的扫描也做了。对外的路由没有接这个参数：

`approve_candidate` 固定调用 `_set_status(..., confirm_sensitive` 的默认值 false`)`。`admin-web/src/views/Candidates.vue` 提交的是 `{}`，没有确认框。

`claim_text` 不是名称字段，词表里的「治疗」「最好」在这里是 review。断言原文里一带这些词，接口就 409，页面没有下一步。CLI 的 `--confirm-sensitive` 可以过，页面这条批不出去。

给这个接口加上和 bundle 相同的请求体字段，页面在 review 命中时先勾选再提交。

### 3.3 输出扫描把绝对化用语和营销词也删掉

`kg/sensitive.py` 的 `rewrite_answer` 在政治类整段替换之后，对每个句子再 `scan` 一次，只要有命中就丢掉整句。词表里的 `最好`、`最佳`、`最有效`、`国家级`、`加微信` 都会命中。计划 §3.7 写的是：这两类只记审计，不改写。

「黑豆和薏米一起吃最好。」会被整句删掉。若回答里每句都有「最好」，正文被删空，函数返回 `MEDICAL_FALLBACK`（「疗效和治病方面给不了建议」）。用户问的是搭配，看到的是疗效拒答。

删句只针对 `medical_claim`。`marketing` 和 `superlative` 留在命中列表里交给 `log_hits`，正文不动。现有单测用的是「治疗」和「法轮功」，盖不住「最好」。

### 3.4 红旗路径的打码没有回到 trace

`memory_write_node` 在落库前会再扫一次政治类。命中时本地变量 `redact = True`，用户消息和标题写占位，画像也跳过。函数返回值只有 `session_id`，没有 `_redact: True`。

`sensitive_guard` 那条路径会把 `_redact` 写进状态，`/chat` 能看见。红旗在 `guard` 就转向 `memory_write`，`sensitive_guard` 不运行。于是「胸痛 + 政治词」的问句：库里是占位，Langfuse 的 input 仍是原句。`api/chat.py` 只在 `result["_redact"]` 时调用 `redact_trace`。

同一处还有顺序问题：政治类那条路径会先 `redact_trace` 把 output 写成占位，随后 `record_result` 用回答覆盖 output。固定话术里没有原词，所以这条路径搜不到词；它和 message 用的不是同一个占位。`_redact` 为真时，`record_result` 的 output 也应写 `[内容已过滤]`，或者把 `redact_trace` 放到 `record_result` 之后。

落库前扫到政治类时，返回值带上 `_redact: True`。用例 1.13 里「红旗 + 政治词」目前没有端到端测试，补一条：用户侧是占位，助手侧仍是「请尽快就医」，trace 的 input 不含原词。

### 3.5 合并日志写下了原文

`kg/merge.py` 拒绝 block 级边时，`log.error` 带上 `claim_text` 的前 40 字。计划要求审计和日志只留 `term_hash`。`log_hits` 已经按 hash 写了 `sensitive_hit_log`，这条 error 把原文又放进会备份的日志。日志里只留候选 id、类别和 hash。

## 4. 计划写了、代码还没接上

这些不挡「菠菜能治疗贫血吗」这种主路径，但和步骤不一致。

**节点合并没有最后一道扫描。** `merge()` 扫边的 `claim_text`。`merge_nodes()` 把白名单属性（名称、别名、性味、摘要、`scientific_name`）直接 `SET` 进图。步骤 2.5 要求这一侧也扫。有人把 `kg_node_candidate.status` 改成 `approved`，或批准之后词表更新了，节点文本不会再被拦住。边那道闸已经有了，节点要按同样的规则丢弃 block。

**「功效」在名称里拦不住。** 步骤 1.12 要求描述里放行、名称里拦截。词表没有「功效」。`_effective_action` 对名称字段一律升成 block，描述字段用词表里的默认动作。把「功效」加成普通词之后，备注会变成 review，和「功效二字放行」冲突。扫描器需要一档「只在名称拦截、描述忽略」。现在的单测只断言描述放行。

**压缩锁盖不住模型调用。** `compress_if_needed` 在读完增量后就结束 `with db.connect()`，锁随之释放，然后才调用摘要模型，再开一条连接 `UPDATE`。同一会话两个请求的后台线程可以读到同一水位线，后写的那次覆盖前一次的合并结果。步骤 4.7 要的是拿不到锁就跳过。写回时带上读到的旧 `summary_until_msg_id` 条件，对不上就放弃这次写入。

**摘要模型失败仍会推进水位线。** 步骤 4.5：失败不推进水位线，下次重试。`compress_if_needed` 在传入的 `summarize_fn` 抛错时确实不推进，单测走的是这条。默认的 `_summarize` 自己吞掉模型异常，退回拼接截断并当作成功。线上模型 500 时，水位线会前进，那些轮次不再以原文出现在提示词里。模型失败应让 `compress_if_needed` 看到失败，而不是拿到一段拼接文本。

**USDA 落库不写 `sensitive_hits`。** `kg/fetch.py` 的 `_persist` 在 block 时整条不落，review 级只 `log_hits`。bundle 行上的 `sensitive_hits` 仍是 `[]`。列表按这一列计数，营销词出现在 USDA 描述里时，列表上看不出要人工看。block 的拦截本身是对的。

**种子词和完成条件里的例子不一致。** 政治类种子是「台独 / 法轮功 / 达赖」。完成条件里的「毛爷爷」不在表里，含这个词的名称现在会进队列。营销类缺计划 §1.1 点名的「销售 / 电话 / 下单」。这些可以靠 SQL 补，代码不用发版；以当前迁移为准的话，那句完成条件不成立。

**`english_name` 不在 `BUNDLE_FIELDS` 里。** 导入校验会扫它。`approve_bundle` 和 `fetch._persist` 把这个键放进 dict，`scan_obj` 按字段表跳过。绕过 `validate_row` 直接写 bundle 时，英文名上的 block 词批得过。

**`SHORT_TERM_WINDOW` 不参与读取。** 窗口由 `MEMORY_RETAIN_TURNS`（默认 3 轮）决定。改 `SHORT_TERM_WINDOW` 不会改变提示词里的轮数。实施记录里写它「只保留作上限」，代码没有读这个值。

## 5. 连接池

`docs/review` 里上一版只评审了连接池。那一版的两项必须改，以及锁、`invalidate`、`limit_concurrency`、断连用例，都已经在 `db/engine.py`、`app.py`、`graph/nodes.py` 的 `_load_profile` 和 `tests/test_db_pool.py` 里。这次没有新的必须改项。

`memory_write_node` 仍用一个大的 `except Exception` 吃掉落库失败。池在画像阶段打满会 503；打满发生在落库这一下时，用户仍拿到 200，这一轮可能没写入 `message`。这是原先「记忆失败不阻断回答」的行为，和过敏过滤被吞掉不是同一类问题。

## 6. 测试没锁住的步骤

`tests/test_sensitive.py` 覆盖了归一化、名称上的疗效词、描述放行、政治类端到端打码、输出扫描的两种动作，以及 `assert_approvable` 这一个函数。下面几条是计划里的完成条件，当前测试过不了或根本没写：

| 计划里的用例 | 现状 |
|---|---|
| 备注含「治疗」的 bundle，详情可见命中，勾选后批准，审计字段有值 | 页面路径会 409，且没有勾选 |
| 三个批准口各来一次 block | 只测了函数，没有 API / CLI / `_set_status` |
| 「最好」出现在回答里，句子保留 | 会被删掉 |
| 红旗 + 政治词：用户侧占位，助手侧是就医原文，trace 不含原词 | 没有这条用例 |
| 「菠菜能治疗贫血吗」走完 `/chat`：有菠菜、无疗效断言、不是政治拒答 | 只断言 `sensitive_guard` 放行，以及切成三句时的 `rewrite_answer` |
| 5 轮之后第二次压缩仍保留第一次摘要，且并发第二次不覆盖 | 顺序调用有测；锁提前释放没有测 |
| 默认摘要函数在模型 500 时不推进水位线 | 测的是注入的会抛错的函数 |

计划要求测试在服务器上跑。这次评审没有重跑全量 pytest。
