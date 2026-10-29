# v3.1 计划评审

> 对照当前仓库审查 `docs/plan/plan_v3.1.md`。未改代码。
> 日期：2026-10-29。

## 1. 结论

对 `code_review_v3.md` 的核实可以信。五条要改、八条没接上，和代码对得上。连接池没有新问题，这个判断也对。

不能按现在的修法写代码。下面六句会把要修的问题修偏，或者撞上已经评审过的 v4：

1. 拼审核扫描对象时，`source` 不在 item 里。item 上的 `source_desc` 是空字符串，盖掉行上的同名列以后，来源文本会从实时重扫里消失。
2. §四-2 的删句片段和 §五的否定白名单是两条相反的规则。按片段写，「不能治疗」仍会被删。
3. 失败后退避不缩短提示词。水位线停住时，未覆盖原文继续变长。
4. `021` 已经是 `plan_v4.md` 的 `021_web_answer.sql`，`review_v4.md` 按这个号通过了。词表迁移用 `022`。
5. `name_only` 若原样返回给 `validate_row`，备注里的「功效」会变成 warn。类别若标成 `medical_claim`，图谱回答里的「功效」会被删句。
6. `_redacted_kind` 不写进 `RAGState` 会被 LangGraph 丢掉，和上次 `_redact` 同一类故障。

§四-3 的条件写回、§四-5 把 `3.4b` 降成占位不一致、不改 `018`、删掉 `SHORT_TERM_WINDOW`，这四条照做。

## 2. 核实

| 计划里的判断 | 对照代码 |
|---|---|
| 备注命中在列表上有、详情上被空数组盖掉、批准 409 | `import_bundle` 扫的 payload 含 `note`。`get_bundle` 在读 items 之前扫 bundle 行，行上没有 `note`。`BundleDetail.vue` 用 `sensitive_hits_live \|\| sensitive_hits`，`[]` 为真 |
| 候选批准不收确认 | `approve_candidate` 无 body。`Candidates.vue` 通过时提交 `'{}'`。`claim_text` 上的疗效词是 review，`confirm=True` 才能过 |
| 「最好」整段变成疗效兜底 | `rewrite_answer` 对任意命中丢句。词表里 `最好` 是 `superlative/review` |
| 红旗路径 trace 的 input 仍是原句 | 补扫只改局部变量 `redact`，返回值没有 `_redact`。`/chat` 只在 `result["_redact"]` 时改 trace |
| 政治路径的 output 被 `record_result` 写回，但话术不含原词 | `redact_trace` 之后 `record_result` 写入 `answer`。这条是占位不一致 |
| 合并日志带 `claim_text` 前 40 字 | `kg/merge.py` 拒绝 block 边时的 `log.error` |
| `merge_nodes` 不扫节点文本 | 白名单属性直接 `SET`。边上的 `claim_text` 有扫描，节点没有 |
| 重跑 `018` 补不进新词 | 种子是 `ON CONFLICT DO NOTHING`。`scripts/migrate.py` 对已应用文件做校验，改 `018` 会报「文件已变更」 |

## 3. 开工前改这六句

### 3.1 审核扫描：补 `note`，不要用 item 覆盖 `source_desc`

导入 payload 的键是 `source`。`_stage_one` 把它写进行上的 `source_desc`。`_items` 的字段名里没有 `source`。item 里的 `source_desc` 取的是 `payload.get("source_desc")`，导入行没有这个键，值是空字符串。

实时重扫今天已经扫行上的 `source_desc`，来源里的营销词详情页能看见，确认框也会出现。批准被卡死的是只有备注命中的那条：实时结果是 `[]`，把落库的 `sensitive_hits` 盖掉。

拼对象时：

- `note` 从 item 的 `field_name='note'` 取。
- 来源继续用行上的 `source_desc`。不要把 item 的 `source_desc` 写回这个键。
- `get_bundle` 的返回仍是现在这份 bundle（名称、items、状态都在）。在上面增加 `hits_live` / `hits_stored`。仓库里没有 `promote.check`。

`live.length ? live : stored` 只在 live 已经包含备注时才够用。live 非空但缺备注时，仍会丢掉 stored 里的备注命中。

### 3.2 删句：否定按命中判断，窗口要盖住复现的那句

§四-2 的片段是「句中有 `medical_claim` 就丢」。§五是「词前有否定就留」。按片段实现，T3 的「菠菜不能治疗贫血」仍然被删。

切句正则只在 `。！？；` 处断开，逗号不断。若「句中任一处被否定则整句保留」，「菠菜不能治疗贫血，但可以治愈夜盲」会把「治愈」一起放出去。规则写成：逐个疗效命中看否定；这一句里还有没被否定的疗效词，整句仍丢。

「词前 6 字」若实现成命中位置往前截 6 个字，「没有证据表明菠菜能治疗」的窗口是「据表明菠菜能」，里面没有「没有证据」，§五复现的那句仍会被删。T3 目前只写了「不能治疗 / 能治疗」，测完发现不了。把「没有证据表明菠菜能治疗贫血，但含铁较丰富。」写进 T3，窗口按这个句子定。

否定判断放在 `kg/sensitive.py` 里给 `rewrite_answer` 用。`plan_v4.md` 的网页裁剪不调用 `rewrite_answer`，它自己按疗效词删句。白名单只写在一边的话，网页回答仍会删掉「不能治疗」。

### 3.3 摘要失败：退避不管提示词长度

条件写回是对的。锁不要跨在模型调用上。`_summarize` 遇到模型异常应让 `compress_if_needed` 看见失败，水位线不前进。这和单测注入的抛错函数一致。

进程内「失败后跳过 1 到 8 次」只减少模型调用。`load_context` 在水位线未盖住时仍拼接全部更早原文。模型连续失败时，这段原文继续变长，退避改变不了这一点。

未覆盖原文要有上限（轮数或字数，二选一写死）。超出的部分在提示词里用一行占位，库里的消息不动，水位线仍不前进。模型恢复后按原消息重算，占位不算一份成功摘要。

### 3.4 迁移号用 `022`

当前 `migrations/` 最大号是 `020_turns.sql`。`plan_v4.md` 的实施方案写的是 `migrations/021_web_answer.sql`。`review_v4.md` 已按这个文件通过，并要求把 `scene` 注释写在这个文件头上。`plan_v5.md` 勘误表里的 `021_request_triage.sql` 是第三份占用，v5 排在 v4 之后，不能当成「021 还空着」。

v3.1 的词表种子写成 `022_sensitive_word_seed2.sql`。§十第 3 条改成：v4 的网页表、v5 的分拣表都顺延，开工前对一下 `ls migrations/`。不要把「等 v5 再取号」留在 v3.1 里。

「电话」两个字会命中「联系电话」，备注整段进入 review。导流词用更长的说法。`毛爷爷`、`销售`、`下单`、`功效` 可以进 `022`。

### 3.5 `name_only` 在扫描时丢掉，不要流到下游

名称字段今天已经把任意类别升成 block。描述字段走词表里的 `action`。`validate_row` 对非 block 一律 warn。`_effective_action` 若在描述上返回 `name_only`，「健脾功效」会变成待确认，验收「备注通过」不成立。

描述类字段遇到 `name_only` 时，`scan` 不返回这条命中。`rewrite_answer` 按类别删句，不看 action。「功效」的 category 用单独的一类，不用 `medical_claim`，否则「有健脾功效」的回答句会被 B1 删掉。

### 3.6 `_redacted_kind` 声明在 `RAGState` 上

`graph/state.py` 没声明的键，下一个节点和 `/chat` 都读不到。`plan_v4.md` 写新状态时已经注明了这一点。

| 路径 | 谁写入 | input | output |
|---|---|---|---|
| 政治类固定话术 | `sensitive_guard` 写入 `_redact` 和 kind | `[内容已过滤]` | `[内容已过滤]` |
| 红旗，问句里有政治词 | `memory_write` 的返回值带上 `_redact` 和 kind | `[内容已过滤]` | 就医回答 |

`record_result` 先写回答，`redact_trace` 再按 kind 决定是否覆盖 output。红旗这条保留就医回答。kind 缺省时两边都写占位，避免没声明的键把就医回答盖掉——声明补上之后，红旗路径必须带 kind，缺省只是兜底。

## 4. 其余照计划做，补两句验收

条件写回、`merge` 日志只留 hash、USDA 的 review 级写入 `sensitive_hits`、`english_name` 加入 `BUNDLE_FIELDS`、删除 `SHORT_TERM_WINDOW`，按计划做。`SHORT_TERM_WINDOW` 只有 `config.py` 在定义，删掉不影响读取。

`merge_nodes` 扫的是 props 里的 `canonical_name`，加上行上的 `name` 列，以及别名、性味、摘要、`scientific_name`。白名单里没有键 `name`（营养素节点除外）。block 的节点不写入图，也不要标成 `merged`，否则闸只生效一次。T8 会连 Neo4j。服务器上那套是生产图，没有单独的测试图时不要开 `ALLOW_GRAPH_TESTS`。

候选页要分成两档，和 bundle 详情一致：`dst_name` 上的 block 禁用通过；`claim_text` 上的 review 才弹出确认并提交 `confirm_sensitive: true`。只弹一次确认的话，政治类名称点了确认仍是 409。

第三节把 `3.3` 标成 P0，第六节把整个 B 批标成 P1。删句按 P0 做。B 批不是 v4 的前置：`plan_v4.md` 写明网页裁剪不含 `superlative`，L3 不调用 `rewrite_answer`。「最好先焯水」在网页路径上本来就不会被这张表删掉。图谱回答里的「最好」仍然要修。

`246 passed / 13 skipped` 是实施记录里的旧数，计划也没有重跑。验收看新用例是否失败，不看这个总数。
