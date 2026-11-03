# plan_v14 —— 相似答案去重

> 生成时间：2026-11-03
> 上游：[`docs/needs/needs_v14.md`](../needs/needs_v14.md)。第二节和第三节是施工口径。
> 2026-11-03 按 [`review_v14`](../review/review_v14.md) 改。不改结构。补上的是：收敛异常退回前 N 条、数字不同不合并、比全文并压掉空白、精排漏返回的名次按原顺序补上、关掉开关和条数上限的用例、账本只留 0 token 的 span。
> 性质：只写怎么施工。不换集合、不加字段、不重嵌、不改图片路径去重。食疗问答、实体链接、长期记忆、联网检索不动。

---

## 一、范围

| Phase | 内容 | 大致人日 |
|---|---|---|
| A | 按正文二字组合收敛，收满提问条数 | 0.4 |
| B | 精排交出全部候选顺序，问答链路接上收敛 | 0.4 |
| **合计** | | **≈ 0.8 人日** |
| C | 单测，以及服务器上跑这组用例 | 0.3 |

**顺序：A → B → C。** B 依赖 A 的函数。C 覆盖 A 的收条规则和 B 改过的精排条数。评审补上的兜底和用例不另开 Phase。

---

## 二、本期不做

按 `heading_path` 合并、再调一次嵌入模型认释义、比较 Milvus 里的 `dense`、图片提问也收敛、改 `search_question` / `search_picture` 的图片路径去重、改离线 Recall、改前台插图、重嵌、换集合。

二字组合对不上的换说法近义，本期留下。不要为了这一步加模型调用。

图片行参加收敛，比的是它的摘要正文。同一张图的两行仍先按 `image_path` 去掉。不同图、摘要不像的，两张都留。

---

## 三、要动的现有文件

| 位置 | 改什么 |
|---|---|
| `kb/diversify.py`（新增） | 二字组合余弦，数字不同不合并，按顺序收满条数 |
| `kb/rerank.py` | `top_n` 改为候选条数；成功、失败、只回部分名次都不要先截成 `KB_SEARCH_LIMIT`。模块说明和函数说明改成「返回全部候选顺序，截断不在这里」 |
| `kb/workflow.py` | 文本提问先取至少 20 条；`_rerank` 异常退回整表；`_diversify` 包 try/except，异常退回排序后的前 N 条 |
| `config.py`、`.env.example`、`README.md` | 收敛开关和阈值。`KB_RERANK_CANDIDATES` 的注释改成：文本提问的召回条数，精排关掉时也按它取 |
| `tests/test_kb_ask.py` | 精排关闭时，检索条数改为候选条数，返回条数不超过提问条数 |
| `tests/test_kb_v13.py` | 精排用例和收敛用例分开；部分返回要补齐 |

**不动**：`kb/search.py` 的 `_dedupe`、`kb/milvus.py`、`kb/embed_mm.py`、`eval/kb_run.py`、`migrations/029_kb.sql`。

---

## 四、Phase A：收敛

新增 `kb/diversify.py`，提供 `diversify(hits, *, limit, threshold) -> list[dict]`。

比较的是整段 `text`，不截 500 字。精排仍只送前 500 字，两处不要共用一个截断理由。`KB_CHUNK_CHARS` 现在是 480，截 500 碰巧等于全文；以后把切块调大，前 500 字相同、后面不同的两块会被并掉。

比较前先做两步归一：`unicodedata.normalize("NFKC", text)`，再 `re.sub(r"\s+", "", text)`。全角数字和全角标点折成半角，PDF 换行不再把同一句话拆成两个二字组合。空串归一后仍用 `"图"`。不足两个字时，集合里只放这一个字符串，不和更长的文本比出高分。

二字组合是相邻两个字符的集合。两条文本的相似度是交集大小除以两边集合大小的几何平均。另外抽出两边的数字串 `\d+(?:\.\d+)?`，按多重集比较。多重集不同就不合并，余弦再高也不收。套话相同、金额或天数不同的两条标准因此都留下。两边都没有数字，或数字完全相同，仍只看余弦。

顺序按传入的列表，不在这里再排序：

1. 第一条直接留下。
2. 后面每一条，和已经留下的每一条算相似度。数字多重集相同，并且余弦大于等于阈值，就丢掉。
3. 留下的条数达到 `limit` 就停。
4. 列表走完还不够 `limit`，返回已经留下的，不把丢掉的补回来。

`limit` 至少为 1。`hits` 为空时返回空列表。

配置：

| 变量 | 默认 | 作用 |
|---|---|---|
| `KB_DIVERSIFY_ENABLED` | `true` | 关掉则文本提问在排序后直接取前 `KB_SEARCH_LIMIT` 条 |
| `KB_DIVERSIFY_THRESHOLD` | `0.72` | 二字组合余弦达到这个值，并且数字多重集相同，就当成同一条意思 |

`0.72` 表示两边大约七成以上的二字组合相同，用来去掉大段复制。同一章里用词差得开的两段应低于这个值。单测里把阈值写死在调用参数上，不依赖默认值刚好是 0.72。数字护栏负责限额、天数这种只差数字的句子，不靠把阈值再调高。

---

## 五、Phase B：接上问答链路

`kb/rerank.py`：

- 请求里的 `top_n` 改成这次候选的条数，也就是 `len(hits)`，不再用 `KB_SEARCH_LIMIT`。
- 按返回的 `index` 重排。没出现在返回里的候选，按原来的顺序接在后面，分数仍用原来的 `score`。只回了前几名时，其余候选还在，收敛才补得了位。
- 没开精排、没密钥、超时、非 200、返回空、解析后一条都没有：返回原来的 `hits`，不要先切成 6 条。硬超时仍是 3 秒，不重试。精排仍不进 LLM 账本。
- 模块第一行和函数说明改掉「截到提问条数」「返回条数不超过 `KB_SEARCH_LIMIT`」。截断不在这个函数里。

`kb/workflow.py` 的文本提问：

1. `search_question` 的 `limit` 用 `max(KB_RERANK_CANDIDATES, KB_SEARCH_LIMIT)`。候选配得比提问条数还小的时候，池子仍够留给生成的名额，不会静默少给。精排开关只决定要不要调用精排，不决定取多少条。
2. `_rerank_on()` 为真时调用精排。异常时用混合检索的原列表，不要在这里切成 `KB_SEARCH_LIMIT`，并打现有的那条警告日志。
3. `KB_DIVERSIFY_ENABLED` 为真时，用 `span("node", "kb_diversify")` 包住 `diversify`。输入是收敛前的 `chunk_id`，输出是留下的 `chunk_id`。阈值用 `KB_DIVERSIFY_THRESHOLD`，条数用 `KB_SEARCH_LIMIT`。`span` 会在 `llm_call_log` 留一行，token 为 0，和 `kb_rerank` 一样。needs 里的「不写进 LLM 账本」指不计 token、不进费用，不是禁止这一行。验收靠这个 span 看输入输出。
4. 收敛自己再包一层 try/except，和 `_rerank` 一样。异常时 `log.warning` 写明「收敛异常，退回排序后的前 N 条」，返回排序后的前 `KB_SEARCH_LIMIT` 条。非 dict、`text` 不是字符串，都不能让 `/kb/ask` 变成 500。
5. 开关为假时，把当前列表切到 `KB_SEARCH_LIMIT`。这是截断的唯一入口。

图片提问保持 `search_picture(..., limit=KB_SEARCH_LIMIT)`，不精排，不收敛。

`tests/conftest.py` 继续把 `KB_RERANK_ENABLED` 设为 false。收敛默认开着，不在夹具里关掉。它不访问外网。

`test_search_limit_follows_config`：精排关闭时，`search_question` 收到的 `limit` 是 `max(KB_RERANK_CANDIDATES, KB_SEARCH_LIMIT)`。桩不要再返回空列表，改成 10 条互不相似的正文。断言检索条数，并断言接口 `hits` 的长度是 `KB_SEARCH_LIMIT`、顺序是这 10 条的前几条。

`test_rerank_reorders_and_failures_cut` 里 8 条正文现在都是 `"字" * 600`。收敛后只会剩 1 条，不能再断言留下前 2 条。拆开：

- 精排函数本身：`top_n` 等于候选条数。全量桩按名次返回全部。只回了 2 个 `index` 的桩，结果是这 2 条在前、其余 6 条按原顺序接在后面。失败时返回全部原列表。
- 问答链路：候选用互不相似的正文时，精排失败后的结果是混合检索顺序的前 `KB_SEARCH_LIMIT` 条。没有 API key 时不发精排请求，检索条数是 `max(KB_RERANK_CANDIDATES, KB_SEARCH_LIMIT)`。
- 另加一条：`KB_DIVERSIFY_ENABLED` 为 false，桩返回 20 条互不相似的正文，`_retrieve` 的结果长度是 `KB_SEARCH_LIMIT`，顺序是原列表的前几条。

---

## 六、Phase C：测试

不连库的收敛用例标 `no_db`。放在 `tests/test_kb_v14.py`。精排条数和开关的改动仍落在 `tests/test_kb_v13.py`、`tests/test_kb_ask.py` 的原用例里。

至少覆盖：

- 5 条正文几乎相同，后面 15 条彼此不同，也和前面不同。`limit=6` 时结果是 6 条，那 5 条里只保留顺序最前的 1 条，其余 5 个名额是后面的原文，顺序不变。
- 近义簇之外只剩 3 条不同的正文，`limit=6` 时结果是 4 条，不把丢掉的近义条补回来。
- 同一 `heading_path`、正文用词不同的两条都留下。
- 套话相同、数字不同的两条都留下。例如「每人每天 100 元」和「每人每天 120 元」。数字相同、其余几乎相同的两条只留第一条。
- 只差换行或全角数字的两条，归一后按同一条收掉。
- 空 `text` 的多条只留下第一条。
- 阈值高于 1 时一条都不丢，结果切到 `limit`。
- 传入的 `text` 不是字符串时，由 workflow 的兜底接住，不从 `diversify` 里抛出去。函数本身可以假设调用方传入的是字符串；workflow 用例负责兜底。

服务器上先把 `PG_DSN` 指到 `nutri_graph_test`（沿用部署环境里的连接串，只改库名）。不要把连接串写进这个文件。然后：

```bash
cd /opt/code/nutri_graph
/opt/envs/nutri/bin/python -m pytest -q -rs \
  tests/test_kb_v14.py \
  tests/test_kb_v13.py::test_rerank_reorders_and_failures_cut \
  tests/test_kb_ask.py::test_search_limit_follows_config
```

`test_kb_ask.py` 需要数据库。输出里不能有 skipped，否则「取多少条、回多少条」没跑到也会显示通过。不要在开发机起库。不跑重嵌，不跑 `eval.kb_run`。

另做一次不进 Milvus、不调模型的核对：从一份含金额或天数的样例正文取出若干块，两两送进 `diversify`，打印被丢掉的配对。被丢掉的应是重复段落。数字不同的标准还在。有图片摘要时，被丢掉的图片行旁边还留着另一条摘要相近的图，不能只留下无关正文。

---

## 七、验收

- 文本提问的调用追踪里能看到 `kb_diversify`。这一行 token 为 0，不进费用。候选里有大段重复时，送给生成的条目里这段只出现一次，总条数不超过 `KB_SEARCH_LIMIT`。
- 同一章、内容不同的两段可以同时出现。套话相同、数字不同的两条标准可以同时出现。
- 精排超时、精排关掉、收敛函数抛错时仍有回答。条数不超过 `KB_SEARCH_LIMIT`。重复段落不会占满名额。收敛抛错时退回的是排序后的前 N 条。
- 图片提问的条数仍是 `KB_SEARCH_LIMIT`，没有 `kb_diversify`。
- 同一 `image_path` 仍只出现一条。不同图的摘要如果被收成一条，留下的那条仍带自己的图。离线 Recall 的计算路径没有改动。
