# v3.1 计划：`code_review_v3.md` 结论核实与收尾

> 目的：对 `docs/review/code_review_v3.md`（2026-10-29 09:14 版）的每一条结论做**代码级核实**，给出采纳 / 修正采纳 / 不采纳的判定，并把需要修的部分排成 v3.1 这一小版。
> 状态：**本轮不改任何源码**。本文只做核实与计划。
> 核实方式：逐条读当前工作区代码 + 远程只读验证（`python -c`，不写库、不跑会写 Neo4j 的用例）。**没有重跑全量 pytest** —— 评审也没跑，所以下文凡是"测试没锁住"的判断，依据是读用例代码，不是跑出来的。
> 修订：2026-10-29，按 `docs/review/review_v3.1.md`。修法以 §四、§五、§六 的修订句为准。第一节里被改掉的旧修法不再执行。

---

## 一、总体判断

**评审成立。** 主干判断（录入按字段分级、输入侧只扫政治、三个批准口收口、池显式提交、多轮三段读取）与我读到的代码一致，没有虚假陈述；`§3` 五条、`§4` 八条我逐条对照过，**全部有代码依据**，没有一条是"看着像"。

分歧只在**口径和落地方式**上，共五处（见 §四）：

1. `3.3` 的修法表述有坑：写成"删句只针对 `medical_claim`"会实现成"按类别过滤句子"，把「菠菜不能**治疗**贫血，但**最好**少吃」这种同句双命中的句子留下来。
2. `3.1` 卡住批准的是备注。来源正文已经在 bundle 行的 `source_desc` 上，实时重扫今天就能看见。item 里没有 `source`，item 的 `source_desc` 是空字符串，用它覆盖行上的列会把来源扫丢（见 §四-1）。
3. `4.4` 的结论对：模型失败不能推进水位线。进程内退避只减少模型调用，提示词里的未覆盖原文照样变长。未覆盖原文要有字数上限（见 §四-3）。
4. `4.6` 说"可以靠 SQL 补，代码不用发版"——**不对**。`018_sensitive_word.sql` 是 `INSERT ... ON CONFLICT DO NOTHING`，重跑迁移补不进新词。新词走 `022`，不要占用 v4 已经定下的 `021`（见 §四-4）。
5. 评审漏了一条比 `§4` 里几条更值得修的：**句子级剔除会把否定表述整句删掉**（"菠菜不能治疗贫血，但含铁较丰富"→ 整段变成疗效拒答）。这条直接和完成条件 1.14「菠菜要出现在回答里」冲突，远程已复现（见 §五）。

`§5 连接池`「这次没有新的必须改项」——**认同**。上一版评审的项确实都进了代码，我也没找到新的。

---

## 二、核实证据（远程只读）

在 `8.134.253.167:/opt/code/nutri_graph` 上直接调 `kg.sensitive`，只读不写：

| 验证 | 命令要点 | 结果 |
|---|---|---|
| 词表实际内容 | `len(s._load())` / 按词过滤 | **22 条**（与 `018` 种子的 3 + 10 + 5 + 4 一致）；`功效`、`毛爷爷`、`销售`、`电话`、`下单` **均查不到**；`最好`/`最佳` = `superlative/review` |
| 3.3 整句删除 | `rewrite_answer("黑豆和薏米一起吃最好。")` | 输出 = `MEDICAL_FALLBACK`（"疗效和治病方面给不了建议"）→ **完全复现评审描述**，包括"问搭配、答拒答"这个后果 |
| 3.3 部分删除 | `rewrite_answer("黑豆和薏米一起吃，能补肾。最好每天一碗。")` | `黑豆和薏米一起吃，能补肾。` → 第二句被删，第一句保留 |
| 疗效句删除 | `rewrite_answer("菠菜含铁丰富，可以辅助治疗贫血。")` | 整段 → `MEDICAL_FALLBACK`（这一句里"含铁丰富"这个事实也没了） |
| **否定语境**（评审未提） | `rewrite_answer("菠菜不能治疗贫血，但含铁较丰富。")` | 整段 → `MEDICAL_FALLBACK`，**正确的否定回答被当成疗效断言删掉** |
| 同句双词 | `rewrite_answer("没有证据表明菠菜能治疗贫血。菠菜含铁。")` | 只剩 `菠菜含铁。`（这条是对的） |

> 提醒：远程跑的是已部署版本，与工作区同步。上述结论全部来自当前代码，不是推演。

---

## 三、逐条判定

优先级：**P0** = 正常审核/使用会卡住；**P1** = 合规口径或与计划不一致，主路径还能走；**P2** = 口径与清理。

### §3 要改

| # | 评审结论 | 代码事实（我核到的） | 判定 | 优先级 |
|---|---|---|---|---|
| 3.1 | 备注上的 review 命中，详情页看不见、确认不了、批准恒 409 | `import_bundle._stage_one` 用 `scan_obj(payload, BUNDLE_FIELDS)` 落库 → payload 有 `note`，所以 `sensitive_hits` 里**有**备注命中（列表计数对）；`promote.get_bundle:112` 重扫的是 **bundle 行**，行上没有 `note` 列 → `scan_obj` 静默跳过键；`BundleDetail.vue:98` 用 `sensitive_hits_live \|\| sensitive_hits` → `[]` 在 JS 里是真值 → 落库那份被丢掉；`approve_bundle` 从 items 读 `note` → 409 | **采纳** | **P0** |
| 3.2 | 关系候选批准接口不收 `confirm_sensitive` | `api/admin.py:198` `approve_candidate` 无 body，直接 `_set_status(..., confirm_sensitive` 默认 `False`)；`Candidates.vue:72` 通过时提交 `'{}'`；`_set_status` 用 `field_name="claim_text"` 扫 → 默认动作 `review` → 恒 409；CLI `kg/review.py:112` 有 `--confirm-sensitive`，能过 | **采纳** | **P0** |
| 3.3 | 输出扫描把绝对化/营销句一起删 | `sensitive.py:324-330`：政治整段替换后，**只要句子里有任意命中就丢整句**，不看类别；远程已复现 `最好` 导致整段变兜底话术；计划 `§3.7`（`plan_v3.md:663`）明确 `marketing`/`superlative` 只记审计不改写 | **采纳**（删句只看疗效命中，否定按命中判断，见 §四-2、§五） | **P0**（图谱回答；不挡 v4，见 §十） |
| 3.4a | 红旗路径的 `_redact` 没回传，trace 的 input 留原句 | `memory_write_node:630` 命中后只改**局部变量** `redact`，返回 `{"session_id": ...}`；红旗在 `guard` 就转向 `memory_write`（`workflow.py:60`），`sensitive_guard` 不跑；`api/chat.py:80` 只在 `result["_redact"]` 时打码 | **采纳** | **P1** |
| 3.4b | 政治路径先 `redact_trace` 再 `record_result`，output 被覆盖 | `tracing.py:69` 写占位 → `record_result:100` 又写 `answer`。但政治路径的 answer 是 `POLITICAL_ANSWER`，**不含原词**，所以**不是泄露，是占位不一致** | **采纳，但降级**（评审把它和 3.4a 并列，实际严重度不同） | **P2** |
| 3.5 | 合并日志写 `claim_text` 前 40 字 | `merge.py:206-209` 确认；与"日志只留 `term_hash`"（`018` 头注释、`log_hits` 实现）冲突 | **采纳** | **P1**（1 行改动，但口径问题不该留） |

### §4 计划写了、代码没接上

| # | 评审结论 | 代码事实 | 判定 | 优先级 |
|---|---|---|---|---|
| 4.1 | `merge_nodes()` 没有最后一道扫描 | `merge_nodes:88` 直接 `SET n += row.props`。食材白名单的名称键是 `canonical_name`，不是 `name`（`name` 在行上另有一列，营养素节点的 props 才用 `name`）。`merge()` 只扫边的 `claim_text` | **采纳**（扫 `canonical_name` 和行上的 `name`，见 §六-C2） | **P1** |
| 4.2 | 「功效」在名称里拦不住 | 词表根本没有「功效」（远程确认）；`_effective_action` 只做"名称一律升 block"，没有"只在名称拦、描述忽略"这一档。加词 → 备注里「健脾功效」变 review，与 1.12「描述放行」冲突 | **采纳**（`scan` 在描述字段丢掉 `name_only`，类别不用 `medical_claim`，见 §六-C1） | **P1** |
| 4.3 | 压缩锁盖不住模型调用 | `short_term.py:344-382`：`with db.connect()` 里读完增量就退出，锁随事务结束释放；`fn(...)` 在锁外；写回另开一条连接且**无水位线条件** | **采纳**（推荐"条件写回"而非"锁内调用"，见 §四-3） | **P1** |
| 4.4 | 摘要失败仍推进水位线 | `_summarize:63-66` 自己 `except` 掉模型异常，退回拼接截断**当作成功**；`compress_if_needed:386-392` 只判 `if not merged` → 水位线前进 | **采纳**（失败不推进，另给未覆盖原文一个字数上限，见 §四-3） | **P1** |
| 4.5 | USDA 落库不写 `sensitive_hits` | `fetch._persist:220-223` 只 `log_hits`，INSERT 列里没有 `sensitive_hits`；列表按该列计数 → review 级营销词看不见 | **采纳** | **P1** |
| 4.6 | 种子词与完成条件不一致 | 远程确认：`毛爷爷` 不在表；营销类缺 `销售`/`电话`/`下单`（计划 `plan_v3.md:49` 点名） | **采纳**（走 `022`；「电话」太短，不进种子，见 §四-4） | **P1** |
| 4.7 | `english_name` 不在 `BUNDLE_FIELDS` | `BUNDLE_FIELDS` = canonical_name / aliases / scientific_name / source_desc / note / source；`validate_row` 扫 `english_name`（`IMPORT_FIELDS` 有），`approve_bundle:336` 传了 `english_name` 但被 `scan_obj` 跳过 | **采纳** | **P2**（正常路径有入口校验兜着，属于纵深防御） |
| 4.8 | `SHORT_TERM_WINDOW` 不参与读取 | 全仓只有 `config.py:187` 定义 + 文档提及，`load_context` 用 `MEMORY_RETAIN_TURNS` | **采纳** | **P2**（死配置比"少一个配置项"更坏：它看起来能调） |

### §5 / §6

| 结论 | 判定 |
|---|---|
| 连接池无新的必须改项；`_load_profile` 认 `PoolBusy`；`limit_concurrency=10` 在 systemd 里 | **认同**（`deploy/systemd/nutri-graph.service:21` 已核） |
| `memory_write_node` 仍用大 `except` 吞落库失败，与"记忆失败不阻断回答"一致 | **认同保留**，但建议加一行计数（见 §八） |
| `§6` 表格 7 条"测试没锁住"，其中 5 条"过不了"、2 条"没写" | **逐条核过，全部成立**；我另补 3 条（见 §七） |

---

## 四、需要修正的五处

### 1. `3.1`：卡住批准的是备注。来源已经在行上

落库那份扫 payload，键是 `note` / `source`。实时那份扫 bundle 行，键是 `canonical_name` / `source_desc`。两份键名确实不一样。

来源正文已经在行上：`_stage_one` 把 payload 的 `source` 写进列 `source_desc`（`import_bundle.py` 的 UPDATE/INSERT）。实时重扫今天就会扫到这一列，详情页能看见，确认框也会出现。

item 里没有 `field_name='source'`。item 的 `source_desc` 取的是 `payload.get("source_desc")`，导入行没有这个键，值是空字符串。用 item 覆盖行上的 `source_desc`，来源文本会从实时重扫里消失。

批准被卡死的是只有备注命中的那条：行上没有 `note`，实时结果是 `[]`。`BundleDetail.vue` 用 `sensitive_hits_live || sensitive_hits`，空数组在 JS 里是真值，落库那份被丢掉。`approve_bundle` 仍从 item 读到备注，返回 409，页面上没有勾选。

修法：

- `get_bundle` 先读 bundle 行和 items。扫描对象 = 行上的字段 + item 里 `field_name='note'` 的值。来源继续用行上的 `source_desc`。不要把 item 的 `source_desc` 写回这个键。
- 返回体仍是现在这份 bundle（名称、items、状态都在上面）。另加 `hits_live`、`hits_stored`、`word_version_live`、`word_version_stored`。保留 `sensitive_hits` 和 `sensitive_hits_live`。仓库里没有 `promote.check`。
- 前端改成 live 非空用 live，否则用 stored。live 必须已经包含备注。live 非空但缺备注时，stored 里的备注命中仍会被丢掉。

### 2. `3.3`：删句只看没被否定的疗效命中

`marketing` / `superlative` 不决定删不删，仍然进 `hits`，交给 `log_hits("output", ...)`。

同句里又有疗效词又有「最好」时，句子仍要删。不能写成「句子里有非疗效类就保留」。

否定不在这一节用「有疗效词就丢」一刀切。那会把「不能治疗」删掉。完整规则在 §五：逐个疗效命中看否定，这一句里还有没被否定的疗效词，整句仍丢。

```python
hits = scan(text)
if any(h.category == "political" for h in hits):
    return POLITICAL_ANSWER, hits
for sent in split_sentences(text):
    claims = [h for h in scan(sent) if h.category == "medical_claim"]
    if any(not claim_negated(sent, h) for h in claims):
        continue
    kept.append(sent)
```

`claim_negated` 的定义以 §五为准。只粘上面这一段、不实现 §五，T3 过不了。

### 3. `4.3` / `4.4`：条件写回 + 失败不推进 + 未覆盖原文有上限

`4.3` 用条件写回，不把行锁跨在模型调用上。摘要是秒级调用，锁跨在往返上会把同一会话的两次提问串成两次模型调用。

```sql
UPDATE session SET summary=%s, summary_until_msg_id=%s, summary_updated_at=now()
WHERE id=%s AND summary_until_msg_id=%s      -- 对不上就放弃这次写入
```

`4.4`：`_summarize` 在模型异常时抛出（或返回 `None`）。`compress_if_needed` 接住后不推进水位线。现在的默认函数自己吞掉异常、退回拼接，水位线会前进，单测注入的抛错函数盖不住这条。

进程内「失败后跳过 1 到 8 次」只减少模型调用。`load_context` 在水位线没盖住时仍拼接全部更早原文。模型连续失败时，这段原文继续变长，退避改变不了这一点。

未覆盖原文设上限，写进配置：`MEMORY_PENDING_MAX_CHARS=2000`。超出的部分在提示词里用一行「更早对话尚未摘要」。库里的消息不动，水位线不前进。模型恢复后按原消息重算。这行占位不是成功摘要，不写入 `session.summary`。

连续失败计数可以同时做（进程内字典，重启清零），`log.warning` 带上「连续第 k 次」。它不代替字数上限。

### 4. `4.6` 走 `022`，不改 `018`，不收「电话」

`018_sensitive_word.sql` 的种子是 `ON CONFLICT (word, category) DO NOTHING`。重跑 `migrate` 不会插入新词。`scripts/migrate.py` 对已应用文件做校验，改 `018` 会报「文件已变更」。

`021` 不能用。`plan_v4.md` 的实施方案是 `migrations/021_web_answer.sql`，`review_v4.md` 已按这个文件通过。`plan_v5.md` 勘误里的 `021_request_triage.sql` 是第三份占用，v5 排在 v4 之后。

→ 新增 `migrations/022_sensitive_word_seed2.sql`：

```sql
INSERT INTO sensitive_word (word, category, action, severity, note) VALUES
    ('毛爷爷', 'political',     'block',     3, '合规红线（完成条件点名的样例）'),
    ('销售',   'marketing',     'review',    2, ''),
    ('下单',   'marketing',     'review',    2, ''),
    ('功效',   'name_guard',    'name_only', 2, '只在名称拦截，描述与回答放行')
ON CONFLICT (word, category) DO NOTHING;

INSERT INTO sensitive_word_version (version, updated_by, note)
VALUES (2, 'migration-022', '补齐：毛爷爷 / 销售 / 下单 / 功效');
```

「电话」不进种子。两个字会命中「联系电话」，备注整段进入 review。要拦导流时用更长的说法，另开迁移。

「功效」的 category 是 `name_guard`，不是 `medical_claim`。输出扫描按类别删句，标成疗效词会把「有健脾功效」的回答句删掉。`name_only` 在描述字段上的处理见 §六 C1：`scan` 不返回这条命中，不能把 `action='name_only'` 交给 `validate_row`（非 block 一律 warn）。

以后每次改词新建迁移（`023`…），写在该文件头上。不要改 `018`，也不要把这条写进短信文档。

### 5. `3.4b` 降到 P2：两条路径的 output 分开，kind 写进 `RAGState`

政治类路径的 answer 是固定话术，不含原词。`record_result` 把 output 写成话术，是占位不一致，不是泄露。

红旗路径的 output 要留下就医回答。计划 §1.6：那句话复述的是症状词，换成占位会丢掉就医证据。

| 路径 | 谁写入 | input | output |
|---|---|---|---|
| 政治类固定话术 | `sensitive_guard` | `[内容已过滤]` | `[内容已过滤]` |
| 红旗，问句里有政治词 | `memory_write` 的返回值 | `[内容已过滤]` | 就医回答 |

`_redact` 和 `_redacted_kind` 都要声明在 `graph/state.py` 的 `RAGState` 上。没声明的键，下一个节点和 `/chat` 都读不到。上次打码不生效就是这个原因。

调用顺序：`record_result` 先写回答，`redact_trace` 再按 kind 决定是否覆盖 output。kind 缺省时 input 和 output 都写占位。红旗路径的返回值必须带上 kind，缺省不会落到就医回答上。

---

## 五、评审漏掉的一条：否定语境会被整句删掉

远程已复现：

```
'菠菜不能治疗贫血，但含铁较丰富。'  →  '这个我只能按图谱里收录的食材信息来回答…'
'没有证据表明菠菜能治疗贫血。菠菜含铁。'  →  '菠菜含铁。'
```

第一条是正确答案被判成违规。回答里要说「不能治疗」，句子级剔除会把它和「能治疗」同等删掉。删光之后若菠菜只出现在这一句，就会退化成疗效拒答。

切句只在 `。！？；` 处断开，逗号不断。若「任一处被否定则整句保留」，「菠菜不能治疗贫血，但可以治愈夜盲」会把「治愈」放出去。

规则：

- 逐个 `medical_claim` 命中看否定。这一句里还有没被否定的疗效词，整句仍丢。全部疗效命中都被否定，句子保留，仍记审计。
- 否定词：`不能` / `无法` / `不建议` / `没有证据` / `并非` / `不构成`。
- 窗口不是「命中位置往前截 6 个字」。那一截盖不住「没有证据表明菠菜能治疗」（前 6 字是「据表明菠菜能」）。定义写成：否定词的结尾到命中词的开头不超过 6 个字。
- 函数放在 `kg/sensitive.py`，`rewrite_answer` 调用它。`plan_v4.md` 的网页裁剪不调用 `rewrite_answer`，自己按疗效词删句。v4 落地时 `trim_sentences` 调同一个判断，否则网页回答仍会删掉「不能治疗」。

不另调模型改写。覆盖不全（「治不了」「谈不上治疗」）留到观察窗口，不在这一版加词。

验收四句写进 T3：

| 句子 | 结果 |
|---|---|
| 菠菜不能治疗贫血 | 保留 |
| 菠菜能治疗贫血 | 删除 |
| 没有证据表明菠菜能治疗贫血，但含铁较丰富。 | 整句保留（唯一的疗效词被否定，后半句是含铁） |
| 菠菜不能治疗贫血，但可以治愈夜盲。 | 整句删除（「治愈」没有被否定） |

---

## 六、v3.1 实施清单

合计 **约 1.5 人日**，三批，每批独立可发。

### A 批：审核链路（P0，0.5d）

| 项 | 改动 | 验收 |
|---|---|---|
| A1 `3.1` 后端 | `get_bundle` 先读行和 items。扫描对象 = 行字段 + item 的 `note`。来源用行上的 `source_desc`，不用 item 的空 `source_desc` 覆盖。返回体仍是 bundle，另加 `hits_live` / `hits_stored` / 两个版本号。保留 `sensitive_hits` 与 `sensitive_hits_live` | 备注含「治疗」、来源干净：`hits_live` 含备注命中。来源含「加微信」：命中仍在，字段是行上的 `source_desc` |
| A2 `3.1` 前端 | `BundleDetail.vue` 在 live 已含备注的前提下，live 非空用 live，否则用 stored。block 禁用、review 出勾选（这两条已有） | 该 bundle 详情能看到备注命中，勾选后提交 `confirm_sensitive: true`，返回 200 |
| A3 `3.2` 后端 | `ApproveCandidateRequest{confirm_sensitive: bool=False}`；`approve_candidate` 收这个 body（现有页面提交 `'{}'`，默认 false）。`_set_status` 把标志传下去 | 不带确认 → 409 `sensitive_blocked`；review 级带上 → 200。block 级即使带确认仍 409 |
| A4 `3.2` 前端 | `list_candidates` 对 `dst_name`（名称，block）和 `claim_text`（review）都计数。block 禁用通过。review 才 `ElMessageBox.confirm` 列出词，确认后提交 `{confirm_sensitive: true}` | 页面能批出 `claim_text` 带「治疗」的候选。名称带政治词时按钮不可用，确认也过不了 |
| A5 部署 | `npm run build` → `/var/www/nutri_graph/console/`；`systemctl restart nutri-graph` | 线上点一遍 A2 / A4 |

### B 批：输出侧与合规口径（B1 / B2 是 P0，其余 P1 / P2，0.5d）

| 项 | 改动 | 验收 |
|---|---|---|
| B1 `3.3` | `rewrite_answer` 按 §四-2：没被否定的 `medical_claim` 才删句。`marketing` / `superlative` 只进审计 | 「黑豆和薏米一起吃最好。」原样返回。同句有「治疗」和「最好」仍整句删 |
| B2 §五 | `claim_negated` 按 §五。否定词结尾到命中词开头不超过 6 字 | T3 的四句全部按表成立 |
| B3 `3.4a` | `RAGState` 声明 `_redact` 与 `_redacted_kind`。红旗补扫命中时返回两者。政治路径由 `sensitive_guard` 写入 kind | 「胸痛 + 政治词」：message 是占位、answer 是就医原文、trace input 不含原词，`result` 里 kind 还在 |
| B4 `3.4b` | `record_result` 先写回答，`redact_trace` 再按 kind 覆盖 output。kind 缺省时两边都是占位 | 政治类：input/output 都是占位。红旗：input 占位、output 是就医原文 |
| B5 `3.5` | `merge.py:206` 改成 `log.error("边候选 %s 命中 %s 级敏感词（hash=%s），拒绝合并", id, cat, term_hash)` | 日志里搜不到 `claim_text` 片段 |

### C 批：纵深防御与口径清理（P1/P2，0.5d）

| 项 | 改动 | 验收 |
|---|---|---|
| C1 `4.2` | `action='name_only'`。名称字段维持「一律 block」。描述字段上 `scan` **不返回**这条命中（不要把 `name_only` 交回 `validate_row`）。「功效」category=`name_guard`，随 `022` 入库。政治类不受影响 | 备注「清热解暑，有健脾功效」通过且不是 warn。名称「菠菜功效」被拒。回答「有健脾功效」不被删句 |
| C2 `4.1` | `merge_nodes` 扫 props 的 `canonical_name`、行上的 `name`，以及别名、性味、摘要、`scientific_name`。block 不写入图，`merged_ids` 不含它，status 保持 `approved`（不要标成 `merged`）。`log_hits(..., source="merge_nodes")`，日志不写原文 | 直连 PG 改成 `approved` 的 block 节点不进图。再跑一次 merge 仍不进图 |
| C3 `4.3` | 写回加 `AND summary_until_msg_id = <读到的水位线>` | 两个线程并发压缩，只有一次写成功 |
| C4 `4.4` | 模型异常抛出，水位线不前进。`MEMORY_PENDING_MAX_CHARS=2000`，超出用一行「更早对话尚未摘要」，不写入 `session.summary`。进程内失败计数可选，不代替字数上限 | 模型抛错：水位线不变；提示词里未覆盖原文不超过 2000 字；连跑两次不重复调用模型 |
| C5 `4.5` | `fetch._persist` 的 INSERT 带 `sensitive_hits`（review 级写入，block 仍然整条不落） | USDA 抓到带营销词的描述：列表显示「待确认 N」 |
| C6 `4.6`+`4.7`+`4.8` | `022`（毛爷爷 / 销售 / 下单 / 功效，无「电话」）。`BUNDLE_FIELDS` 在 `scientific_name` 后加 `english_name`。删除 `SHORT_TERM_WINDOW` | 迁移可重跑。描述里的「功效」不产生命中。改 `SHORT_TERM_WINDOW` 这个名字不再存在 |

> `4.8` 选"删"而不是"改成上限"：`MEMORY_RETAIN_TURNS` 已经是唯一事实来源，留一个名字相近的第二旋钮，下一个人一定会去调错那个。

---

## 七、测试补齐

评审 `§6` 的 7 条我全部认同，另补 3 条（B2 / A3 / C3 各一条已在上面列了验收，这里只补评审没提的）：

| # | 用例 | 归属 |
|---|---|---|
| T1 | 备注含「治疗」的 bundle：`get_bundle` 的 `hits_live` 非空；`_set_status` 无确认 → 409，有确认 → 200 | A 批 |
| T2 | 「最好」出现在回答里 → 句子保留；「最好」+「治疗」同句 → 整句删 | B 批 |
| T3 | §五的四句：不能治疗保留；能治疗删除；「没有证据表明菠菜能治疗贫血，但含铁较丰富。」整句保留；「不能治疗，但可以治愈夜盲」整句删除 | B 批（§五） |
| T4 | 红旗 + 政治词：user 侧占位、assistant 侧就医原文、trace input 无原词 | B 批 |
| T5 | 「菠菜能治疗贫血吗」走完整 `/chat`：有菠菜、无断言、不是政治拒答 | B 批（评审已列，目前只有半条） |
| T6 | 并发压缩：两线程同时 `compress_if_needed` → 只写一次，第二次放弃 | C 批 |
| T7 | 默认 `_summarize` 在 `invoke_model` 抛错时不推进水位线；未覆盖原文超过 2000 字时提示词截断，`session.summary` 不被占位覆盖 | C 批 |
| T8 | `merge_nodes` 拦下 block 节点，`merged_ids` 不含它，status 仍是 `approved`。要 `ALLOW_GRAPH_TESTS=1`。服务器上的 Neo4j 是生产图，没有单独测试图时不要开这个开关 | C 批 |
| T9 | `022` 之后词表含毛爷爷 / 销售 / 下单 / 功效，没有「电话」。「功效」在名称拦、在描述放行、在回答里不删句 | C 批 |
| T10 | 前端不能单测的部分：用 A2/A4 的接口返回断言"两个字段都有值时页面拿得到"（Vue 侧靠手工点一遍） | A 批 |

`246 passed / 13 skipped` 是实施记录里的旧数，这份计划没有重跑。验收看 T1–T10 是否按表失败或通过，不看这个总数。T8 默认跳过。

---

## 八、明确不改的

| 项 | 理由 |
|---|---|
| 连接池（`§5`） | 上一轮的项都在代码里，本轮没找到新问题 |
| `memory_write_node` 的大 `except` | "记忆失败不阻断回答"是既定行为，和"画像被吞"不是一类（画像影响的是过敏过滤，属于安全问题；记忆失败只是丢一轮历史） |
| `rewrite_answer` 的兜底话术文案 | `MEDICAL_FALLBACK` 本身没问题，问题是**什么时候**用它（`3.3` + §五 修完，它只会在"确实整段都是疗效断言"时出现） |
| 不接第三方内容审核 | `CONTENT_MODERATION_PROVIDER` 仍是常量 + 注释，`plan_v3.md §1.8` 已划在 v3 之外 |

**建议加的一行观测**（不改行为）：`memory_write_node` 的 `except` 里 `log.warning` 带上 `metric=memory_write_failed`，方便 `journalctl | grep -c` 数"这一轮没写进历史"的比例。否则 `§5` 提到的那个"池在落库这一下打满 → 200 但没写入"永远发现不了。

---

## 九、上线与观察窗口

三批分开发，每批观察 24h。观察项（就是之前挂着没做的那几条，这次一并固化成 SQL，放进每周一跑的 report 里）：

```sql
-- 1) review 档的通过 / 驳回比例（决定要不要按 §3.6 收紧）
SELECT h.term_hash, h.category,
       count(DISTINCT b.id) FILTER (WHERE b.status IN ('approved','merged')) AS passed,
       count(DISTINCT b.id) FILTER (WHERE b.status = 'rejected')            AS rejected,
       count(DISTINCT b.id)                                                  AS bundles
FROM sensitive_hit_log h
JOIN kg_food_bundle b ON b.id = h.bundle_id
WHERE h.scene IN ('import','review') AND h.created_at > now() - interval '7 days'
GROUP BY 1,2 ORDER BY bundles DESC;

-- 2) 输出侧被改写的频率（B 批之后这条应该只在真断言上出现）
SELECT date_trunc('day', created_at) AS d, category, action, count(*)
FROM sensitive_hit_log WHERE scene = 'output' AND created_at > now() - interval '14 days'
GROUP BY 1,2,3 ORDER BY 1 DESC;

-- 3) 词表版本与命中是否对得上（有人手工改库会在这里露出来）
SELECT word_version, count(*) FROM sensitive_hit_log
WHERE created_at > now() - interval '7 days' GROUP BY 1 ORDER BY 1;
```

池满：`journalctl -u nutri-graph --since '1 day ago' | grep -c pool_busy`（现在没有计数指标，先用日志数；连续 3 天为 0 才认为并发上限合适）。

误杀反馈：B 批上线后，问「菠菜能治疗贫血吗」这类问题，看 answer 是不是拒答；一周内出现 3 次以上，说明 §五 的否定白名单不够，需要扩。

---

## 十、与 v4 / v5 的关系（排期约束）

1. **B1 修的是图谱回答，不挡 v4 开工。** `plan_v4.md` 写明网页裁剪不含 `superlative`，L3 不调用 `rewrite_answer`。「最好先焯水」在网页路径上不会被这张表删掉。图谱回答里的「最好」仍然要修。v4 落地时，`trim_sentences` 调用 §五 的 `claim_negated`，网页回答和图谱回答对「不能治疗」用同一条规则。
2. **A3（`approve_candidate` 收 body）与 v5 有交叠。** v5 要改造收录工作台，会碰 `api/admin.py` 的候选接口。A3 先落，v5 基于新签名写。
3. **迁移号：v4 用 `021_web_answer.sql`，v3.1 用 `022_sensitive_word_seed2.sql`。** `review_v4.md` 已按 `021` 通过。`plan_v5.md` 勘误里的 `021_request_triage.sql` 在 v4 的 `021` 落地后顺延，v5 开工前再 `ls migrations/`。v3.1 不占用 `021`。

---

## 十一、这份文档的结论，一句话

按 `review_v3.1.md` 修过的句子实施：备注从 item 取、来源留在行上的 `source_desc`；删句只丢没被否定的疗效命中；未覆盖原文最多 2000 字；词表走 `022`，不收「电话」；`name_only` 在描述字段不返回命中，「功效」用 `name_guard`；`_redacted_kind` 声明在 `RAGState` 上。条件写回、日志只留 hash、不改 `018`、删除 `SHORT_TERM_WINDOW` 保持不变。
