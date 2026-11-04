# plan_v15 —— 相邻块补齐

> 生成时间：2026-11-04
> 上游：[`docs/needs/needs_v15.md`](../needs/needs_v15.md)。第二节和第三节是施工口径。
> 2026-11-04 按 [`review_v15`](../review/review_v15.md) 改。不改结构。补上的是：`run_ask` 用 `score` 的空值保护，并把 `neighbor` 透传出去；邻居行必须带齐 `chunk_id`、`block_kind`、`text`、`heading_path`。
> 性质：只写怎么施工。不改嵌入，不改 Milvus 集合，不重嵌全库，不改召回条数。食疗问答、实体链接、长期记忆、联网检索不动。

---

## 一、范围

| Phase | 内容 | 大致人日 |
|---|---|---|
| A | `kb_chunk` 增加序号，入库时按阅读顺序写入 | 0.3 |
| B | 按序号和章节把前一块、后一块拼到命中列表上 | 0.3 |
| C | 文本提问在近义收敛之后接上这一步 | 0.3 |
| **合计** | | **≈ 0.9 人日** |
| D | 单测，以及服务器上迁移和跑这组用例 | 0.3 |

**顺序：A → B → C → D。** B 的拼接不访问数据库。C 依赖 A 的读接口和 B 的拼接。D 覆盖旧块没有序号时的原样返回。

---

## 二、本期不做

句子窗口（把相邻正文抄进同一条向量记录）、把 Milvus 召回从 20 改成 60、再用问题搜一次邻居、跨 `heading_path` 补块、把同一章节的全部块交给模型、邻居参与精排或近义收敛、图片提问补邻居、改 `search_question` / `search_picture`、改离线 Recall、全库重嵌、改前台插图规则、改 480 字切块。

同一张图的图像行和描述行仍先按 `image_path` 去重。补邻居时，与命中块 `image_path` 相同的另一行跳过。

---

## 三、要动的现有文件

| 位置 | 改什么 |
|---|---|
| `migrations/033_kb_chunk_seq.sql`（新增） | `kb_chunk.seq`，可空整数。同一 `doc_id` 上 `(doc_id, seq)` 唯一 |
| `kb/publish.py` | 写入行时按列表下标填 `seq`，从 0 开始。图像行和描述行各占一个序号 |
| `kb/store.py` | `replace_chunks` 写入 `seq`。新增按文档和序号读取相邻块 |
| `kb/neighbor.py`（新增） | 纯拼接：给定命中和已读出的邻居，排出前一块、命中、后一块 |
| `kb/workflow.py` | 文本提问在 `_diversify` 之后调用补齐。异常退回收敛结果 |
| `config.py`、`.env.example`、`README.md` | `KB_NEIGHBOR_ENABLED`，默认 `true` |
| `tests/test_kb_v15.py`（新增） | 拼接规则，以及序号为空时不补 |
| `docs/文档处理全链路流程.md` | 检索一节补上「收敛之后按序号读相邻块」 |

**不动**：`kb/search.py`、`kb/milvus.py`、`kb/rerank.py`、`kb/diversify.py`、`kb/embed_mm.py`、`kb/chunk.py`、`eval/kb_run.py`、`kb-web/`。

---

## 四、Phase A：序号

`migrations/033_kb_chunk_seq.sql`：

- `ALTER TABLE kb_chunk ADD COLUMN IF NOT EXISTS seq INT`
- 唯一索引：`doc_id, seq`。`seq` 为空的多行允许并存，PostgreSQL 唯一索引本来就放过 NULL
- 已有行保持 NULL。不在这条迁移里填序号

`kb/publish.py` 在 `_index` 得到的 `rows` 上，按这个列表的顺序写 `seq`：`0, 1, 2, …`。这个顺序就是 `chunk.prepare` 之后、一张图拆成两行之后的阅读顺序。`replace_chunks` 把 `seq` 一并插入。重嵌仍是整篇删除再插入，序号跟新块一起重编。

不要用 `page_no`、`bbox` 或 `chunk_id` 代替这个序号。

`kb/store.py` 提供按命中读取邻居的函数，一次查询拿全：

- 入参是收敛后的命中（含 `chunk_id`）
- 先读出这些 `chunk_id` 的 `doc_id, seq, heading_path, image_path`
- `seq` 为空的命中不产生邻居
- 对其余命中，取同一 `doc_id`、`seq` 为当前值减一或加一、且 `heading_path` 相同的行
- 返回命中 id 到「前一块、后一块」的映射。某一侧不存在就是空

邻居行要带上生成和前台需要的字段：`chunk_id`、`block_kind`、`text`、`heading_path`、`image_path`、`doc_id`。`text` 必须选出。`run_ask` 对这四个正文键仍是直接取值，缺任何一键都会在返回前抛错。不读 Milvus，不嵌向量。

---

## 五、Phase B：拼接

新增 `kb/neighbor.py`，提供 `attach(hits, neighbors, *) -> list[dict]`。`neighbors` 是 Phase A 已经读好的映射，本函数不再查库。

按 `hits` 的现有顺序逐条处理：

1. 前一块存在、`heading_path` 与命中相同、`image_path` 为空或不等于命中的 `image_path`、并且 `chunk_id` 还没写进结果，就先放入结果。放入的字典带 `neighbor: true`，不带 `score`。
2. 放入命中块本身。命中块不标 `neighbor`。
3. 后一块按同样的条件放在命中块后面。

`image_path` 相同的跳过，是为了去掉同一张图的另一行。`heading_path` 的过滤在查询里做一层，这里再做一层，避免调用方把跨章节的行传进来。

`hits` 为空时返回空列表。映射里没有这条命中时，原样留下这一条。同一 `chunk_id` 先出现的位置保留，后面的邻居或另一条命中不再重复插入。因此两条相邻命中不会把中间那块写两遍，也不会把命中块再标成邻居。

本函数不截断到 `KB_SEARCH_LIMIT`。6 条命中各带两侧新邻居时，结果是 18 条。这是交给模型的上限，不是召回条数。

---

## 六、Phase C：接上问答链路

`KB_NEIGHBOR_ENABLED` 默认 `true`。为 false 时，`_retrieve` 在收敛之后直接返回，行为与现在相同。

为 true 时，只在文本提问上、`_diversify` 返回之后调用。图片提问不调用。用 `span("node", "kb_neighbor")` 包住。输入是收敛后的 `chunk_id`，输出是补上的邻居 `chunk_id`。这一行 token 为 0，不进费用。

再包一层 try/except。读库或拼接抛错时，`log.warning` 写明「相邻块补齐失败，只保留收敛结果：%s」，返回收敛结果。不能让 `/kb/ask` 变成 500。

`run_ask` 组装返回值时：

- `score` 改成 `hit.get("score")`。现在是 `hit["score"]`，邻居字典不带这个键，会在返回前 `KeyError`。命中块仍有分，行为不变。
- `neighbor` 为 true 时写入返回项。现有组装只有固定的几项，不写这句的话前台看不到相邻标记。
- 有 `image_path` 的邻居仍按现有规则填 `image_url`。

生成提示继续用 `format_hit`。它已经用 `get` 读正文，缺 `text` 不会在这里抛错，但会变成空依据，所以上一步的查询仍必须选出 `text`。邻居和命中块是同一种 `[章节] 正文`。

`_images` 会把 `block_kind == "image"` 的邻居记进 `kb_image`。文本提问不因此改走图片检索。这和「邻居自己有图就按章节展示」一致，不用把图像行从邻居里排除。

`tests/conftest.py` 不要关掉 `KB_NEIGHBOR_ENABLED`。序号为空或库里没有这条块时，补齐结果等于收敛结果，现有「返回 6 条」的用例仍然成立。

---

## 七、Phase D：测试

不连库的拼接用例标 `no_db`，放在 `tests/test_kb_v15.py`。至少覆盖：

- 一条命中，前后块同章，结果是前一块、命中、后一块。前后块带 `neighbor: true`，命中块不带。
- 后一块的 `heading_path` 不同：不补后一块。
- 后一块与命中的 `image_path` 相同：不补后一块。
- 两条命中相邻，中间块只出现一次，且不标成邻居。
- 第一块没有前一块、最后一块没有后一块：缺的一侧不出现。
- 映射里没有这条命中：原样返回。
- `KB_NEIGHBOR_ENABLED` 为 false：`_retrieve` 不读邻居，条数仍是 `KB_SEARCH_LIMIT`。
- 读邻居的函数抛错：`_retrieve` 返回收敛结果，不抛出。
- 邻居没有 `score`：`run_ask` 不抛错，返回里该条 `score` 为空、`neighbor` 为 true。

服务器上先迁移，再把 `PG_DSN` 指到 `nutri_graph_test`（沿用部署环境里的连接串，只改库名）。不要把连接串写进这个文件。然后：

```bash
cd /opt/code/nutri_graph
/opt/envs/nutri/bin/python -m scripts.migrate
/opt/envs/nutri/bin/python -m pytest -q -rs \
  tests/test_kb_v15.py \
  tests/test_kb_ask.py::test_search_limit_follows_config
```

迁移要在服务使用的库和 `nutri_graph_test` 上都执行。`test_kb_ask.py` 需要数据库。输出里不能有 skipped。不要在开发机起库。不跑重嵌，不跑 `eval.kb_run`。

另做一次不调模型的核对：找一篇重嵌之后带序号的文档，抽一块同章的命中，打印它的 `seq` 和补上的两块 `chunk_id`。两块的序号应是命中序号减一和加一，章节相同。没有序号的旧块打印出来应是「不补」。

---

## 八、验收

- 文本提问的调用追踪里能看到 `kb_neighbor`。这一行 token 为 0。Milvus 召回条数仍是大约 20，不是 60。
- 有序号、且前后块同章时，生成提示里命中块的前一块和后一块按阅读顺序贴在它两侧。接口 `hits` 里这两块带 `neighbor: true`，没有检索分。总条数可以大于 `KB_SEARCH_LIMIT`，不超过 18。
- 章节不同的邻块不出现。同一张图的另一行不出现。重叠的邻居只出现一次。
- 序号为空的旧文档，回答仍是收敛后的最多 6 条。本期没有全库重嵌。
- 补齐抛错、或开关关掉时，仍按收敛结果回答。
- 图片提问没有 `kb_neighbor`。离线 Recall 的计算路径没有改动。
