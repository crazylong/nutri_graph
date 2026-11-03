# plan_v13 —— 图片摘要两行入库与文本精排

> 生成时间：2026-11-03
> 上游：[`docs/needs/needs_v13.md`](../needs/needs_v13.md)。第二节是施工口径。第四节评审除「只换图片行」外都采纳；重嵌走全量（4.6 已拍板）。
> 2026-11-03 按 [`review_v13`](../review/review_v13.md) 三轮改。第三轮不再改结构。补上的是：Phase F 三条单测、追加题按问题去重、`--limit` 按有效题计数、重嵌后图片地址要返回 200、渲染 dpi 用一个常量。
> 性质：只写怎么施工。不换集合、不加向量字段、不改 1152 维、不做融合向量。食疗问答、实体链接、长期记忆、联网检索不动。

---

## 一、范围

| Phase | 内容 | 大致人日 |
|---|---|---|
| A | 摘要带前后文，截断 300 字 | 0.5 |
| B | PDF 按坐标把图插回正文 | 0.6 |
| C | 一张图写两行独立向量 | 0.5 |
| D | 检索按图片路径去重；Recall 认兄弟行 | 0.4 |
| E | 文本提问精排 | 0.6 |
| F | 全量重嵌脚本 | 0.5 |
| **合计** | | **≈ 3.1 人日** |
| G | 服务器上导入带图 PDF、同一套题重嵌前后各评一次、pytest | 0.8 |

**顺序：A → B → C → D → E → F → G。** C 依赖 A 写好的摘要。D 依赖 C 的两行。E 依赖 D 的去重结果。F 在前五步之后才能重嵌。G 的「重嵌前」分数和块快照要在执行重嵌之前记下。

---

## 二、本期不做

换集合名、加第二个向量字段、改成 gme_rag 的 1024 维和 `IP`、`image` 与摘要打进同一次嵌入、食疗线精排、给图片提问先写描述再精排、跨文档取前后文、摘要缓存、旧图片链接兼容、修 PDF 抽不出矢量图的问题。

一张图两行，是因为现有集合一行只有一个 `dense`。不要改成一行两个向量字段。

---

## 三、要动的现有文件

| 位置 | 改什么 |
|---|---|
| `kb/caption.py` | 三档提示词，前后文入参，截断 300 字 |
| `kb/chunk.py` | 切块结束后给图片块补 `prev_text` / `next_text` |
| `kb/parse_pdf.py` | 按页把图插进正文顺序；像素坐标换算成点 |
| `kb/publish.py` | 图片块写两行；新增 `reindex`，不复用 `approve` |
| `kb/milvus.py` | 混合检索按调用方要的条数多取一些，供去重 |
| `kb/search.py` | 返回前按 `image_path` 去重 |
| `kb/rerank.py`（新增） | 百炼文本精排原生接口 |
| `kb/workflow.py` | 文本提问先取 20 条，精排后留 6 条 |
| `config.py`、`.env.example`、`README.md` | 精排地址、候选条数、开关 |
| `eval/kb_run.py` | `_recall` 再查一次 `image_path`，兄弟行算命中。不改 `_tenant` 给 Ragas 用的返回值 |
| `scripts/gen_kb_samples.py` | `image_path IS NULL`；增加 `--doc-id`（可重复）和 `--append` |
| `scripts/reembed_kb.py`（新增） | 逐篇重嵌；快照写到 `data/snapshots/`；按固定排序重映射 `kb_pairs.jsonl` |
| `tests/conftest.py` | 默认关掉精排 |
| `tests/test_kb_*.py` | 见第十节 |

**不动**：`kb/embed_mm.py` 的独立向量拒绝逻辑、食疗集合 `kg_entity` / `kb_chunks`、Milvus 集合名 `kb_mm_tongyi_vision_plus_1152`、`migrations/029_kb.sql` 的表结构。

---

## 四、Phase A：摘要

`caption_bytes` 增加 `prev_text=""`、`next_text=""`。提示词按参考项目 `generate_image_description` 分三档：前后都有、只有一侧、两侧都没有。三档都要求描述不超过 300 个汉字，并结合能看到的上下文说明这张图在文档里是什么。模型仍是 `KB_CAPTION_MODEL`。

返回截断 `text[:300]`。空结果仍用 `图:{filename}`，后面的摘要行还要入库。送进模型的前后文各自再截到 500 字。模块说明改成：描述写入 `text`，并另嵌一条文本向量；稠密字段里不放融合向量。

前后文在 `chunk.prepare()` 收尾时填，不在解析器里填。对每个 `block_kind == "image"` 的块，向前、向后各找最近一个 `block_kind == "text"` 的块，跳过其它图片，各取一块全文再截 500 字。没有的一侧留空串。`page_no` 有值且 `bbox` 为空的图是没有坐标的 PDF 图，两侧都留空，只让描述使用章节路径。Markdown、DOCX 的 `page_no` 本来就是空的，不受这条影响。不另加 `no_context` 字段。

认正文只看 `block_kind`。这一步图片路径还没写。

---

## 五、Phase B：PDF 插图

只处理 PDF。Markdown、DOCX 的图已经在段落之间，不改顺序。

`_from_pymupdf`：一页的正文行保持抽出的顺序。有坐标的图插到「本页最后一条 `bbox[1]` 小于该图 `bbox[1]` 的正文」后面。没有这样的正文时，插到本页正文之前。插完后，图的 `heading_path` 用它前面那条正文的章节路径；插在页首时用当时已经生效的标题。不要把整页正文按 y 重排。

没有坐标的图仍放在该页正文之后，`bbox` 留空。`page_no` 仍写页号。Phase A 用这两个字段判断不取前后文。

`dots_mocr` 路径：版面文字的 bbox 是渲染图上的像素，PyMuPDF 抽出的图是 PDF 点。模块里只写一处 `_RENDER_DPI = 144`，`get_pixmap` 和换算都用它，换算系数是 `72 / _RENDER_DPI`。在写成 block 时就把版面 bbox 乘这个系数，比较和入库用的都是 PDF 点。图仍来自 PyMuPDF，按上面的规则插进已经换算过的正文列表，不再整篇追加到文末。版面结果里的 Picture 继续跳过，避免同一张图出现两次。从这一版起，`kb_chunk.bbox` 统一是 PDF 点。重嵌前的旧行还是像素，全量重嵌之后已通过文档都换成点。

---

## 六、Phase C：一图两行

`publish._index` 里，正文块仍是一行：`dense = embed_text(正文)`。

图片块产出两行，摘要用 Phase A 的结果，两行 `text` 都是这同一段：

| | 图片行 | 摘要行 |
|---|---|---|
| `chunk_id` | 新的 uuid，图片文件 `{chunk_id}{ext}` 用这个 id | 另一个 uuid |
| `block_kind` | `image` | `text` |
| `image_path` | 该文件的路径 | 同一路径 |
| `image_md5` | 图片内容的 md5 | 同一值 |
| `dense` | `embed_image_bytes` | `embed_text(摘要)` |
| 其余 | `page_no`、`bbox`、`heading_path` 两行相同 | 同左 |

`/kb/images/{chunk_id}` 按 `kb_chunk.image_path` 读文件，摘要行的 id 也能打开同一张图。`kb_chunk` 两行都写。Milvus 字段不增。

嵌入失败时保持现在的行为：删掉该文档本次已写的向量和块，状态留在待审核。

---

## 七、Phase D：去重

去重放在 `kb/search.py` 的 `search_question` 和 `search_picture` 返回前，问答和离线评估走同一处。

`image_path` 为空的行不合并。有路径的行按路径分组，留 `score` 高的一条，其余丢掉。组内顺序按第一次出现的位置，避免打乱混合检索的先后。

`milvus.search_text` / `search_image` 的 `limit` 仍是调用方最终要的条数。内部先取 `fetch = max(limit * 2, 20)` 条再去重，然后截回 `limit`。`search_text` 里三处一起改成 `fetch`：稠密请求、稀疏请求、`hybrid_search`。只改最后一处的话，融合前没有多余候选，去重后会不够。`search_image` 只有一次 `client.search`，它的 `limit` 也用 `fetch`。不要把对外的 `limit` 写死成 20，否则 `eval.kb_run` 传入的 10 会变成 20。

`eval/kb_run.py` 的 `_recall`：`chunk_id` 对上仍算命中。对不上时，按标准块 id 再查一次 `image_path`（`_tenant` 继续只返回租户，Ragas 那条调用不用改）。有路径时，结果里同一路径的那条算命中，名次用那条的名次。标准块不存在时仍退出。重嵌之后不重出题，用 Phase F 的快照把旧 id 映射成新 id。

`scripts/gen_kb_samples.py` 的正文条件写成 `c.image_path IS NULL`。`replace_chunks` 把空路径存成 NULL，写成 `= ''` 筛不到。摘要行的 `block_kind` 也是 `text`，不过滤的话题目会出在图片描述上。注释里写上这个原因。

另加 `--doc-id`（可重复）和 `--append`。`--doc-id` 只从这些文档里取正文块。`--limit` 是要得到的有效题数，不是扫描的块数。短于 20 字的块跳过，继续往后取，直到凑够或块用完。`--append` 把新题追加到现有 `kb_pairs.jsonl`，不覆盖旧题，也不重写 `kb_qa.jsonl`。已有相同 `question` 的跳过并打印条数，避免补题命令跑两次把同一题算进两次 Recall。不带 `--append` 时仍是整文件覆盖。本期给 4 份 PDF 补题用 `--append`，见 Phase G。

---

## 八、Phase E：精排

新增 `kb/rerank.py`。请求：

- 地址默认 `https://dashscope.aliyuncs.com/api/v1/services/rerank/text-rerank/text-rerank`，配置项 `RERANK_BASE_URL`。不要用 `DASHSCOPE_BASE_URL`，那个 compatible-mode 下面没有这个接口。
- 模型名 `RERANK_MODEL_POOL[0]`。
- 正文是问题，文档是每条候选的 `text` 前 500 字。`return_documents` 为 false，`top_n` 为 `KB_SEARCH_LIMIT`。
- 按返回的 `index` 重排，`score` 改成 `relevance_score`。日志里写明这是精排分。

配置：

| 变量 | 默认 | 作用 |
|---|---|---|
| `KB_RERANK_ENABLED` | `true` | 关掉则文本提问直接按 `KB_SEARCH_LIMIT` 检索，不精排 |
| `KB_RERANK_CANDIDATES` | `20` | 精排前的候选条数 |
| `RERANK_BASE_URL` | 上面的原生地址 | 精排端点 |

`kb/workflow.py` 的文本提问：开关打开时 `search_question(..., limit=KB_RERANK_CANDIDATES)`，再精排，结果截成 `KB_SEARCH_LIMIT`。用 `span("node", "kb_rerank")` 包住这次调用。下面几种情况都按混合检索原顺序取前 `KB_SEARCH_LIMIT` 条，不把 20 条送给生成：

- `RERANK_MODEL_POOL` 为空，或 `DASHSCOPE_API_KEY` 为空。没 key 时直接跳过，不要先请求再等超时。
- 超时。硬超时 3 秒，不重试。
- 非 200，或返回空。

返回里的 `usage` 打进这条 span 的日志。精排不进 LLM 账本，费用页看不到它。图片提问只调 `search_picture(..., limit=KB_SEARCH_LIMIT)`，不精排。

精排分不读 `CHUNK_THRESHOLD`，也不写进前端。

`tests/conftest.py` 加自动夹具，把 `KB_RERANK_ENABLED` 设为 false。现有 `test_search_limit_follows_config` 继续断言检索条数等于 `KB_SEARCH_LIMIT`。精排用例自己打开开关，并桩掉 `kb.rerank`，不访问百炼。

---

## 九、Phase F：全量重嵌

`_index` 把「写到哪」和「记哪条路径」分开。审核通过时两个目录都是 `doc_dir/images/`。重嵌时字节写到 `doc_dir/images.new/`，行里的 `image_path` 写成换名之后的 `doc_dir/images/{chunk_id}{ext}`。不要把 `images.new` 写进库。

`publish.reindex(doc_id)`：

1. 文档必须存在。不走 `approve()`。`approve()` 对已通过文档直接返回。
2. 解析、摘要、嵌入，图片字节落到 `images.new/`，路径记成 `images/`。失败时删掉 `images.new/`，**状态保持已通过**，`error` 写成「重嵌失败：…」。旧向量、旧块、旧图都不动。检索不看审核状态，这时若改成待审核，文章仍会被搜到，图片接口却因状态不是已通过而 404。
3. `milvus.delete_doc`，删掉该文档的 `kb_chunk`。删掉 `doc_dir/images/`，再把 `images.new/` 换名为 `images/`。源文件留着。
4. 换名之后检查每个 `image_path` 都是存在的文件。缺一个就按已删旧行处理：清掉半成品向量和块，状态改为待审核。换名之前不要做这次检查，那时 `images/` 下还没有新文件。
5. 写入新的 Milvus 行和 `kb_chunk`。这一步失败同样改为待审核，并清掉本次已写入的向量和块。
6. 成功则状态保持已通过。`error` 写成这次的告警（例如图大于 5MB），用「；」连接。没有告警才写成空串。不要无条件清空，那会抹掉这次的告警；也不要留着上一轮审核的旧告警。

一篇文档在删旧行和写新行之间检索为空。脚本一篇一篇串行，不并发同一篇。`mark` 会刷新 `updated_at`，后台按更新时间排序会变动，文档并没有被换成另一篇。

`scripts/reembed_kb.py`：

- 默认处理全部 `status=approved` 的文档，一篇失败继续下一篇。每篇打出抽出的图数。
- `--doc-id` 只跑一篇。`--dry-run` 只解析并打印每篇的图片数、其中有坐标的张数，不调描述和嵌入，也不删行。开头说明这次会把 PDF 再解析一遍。验收步骤不跑它，避免 `dots_mocr` 对同一批 PDF 做两次 OCR。
- `--snapshot` 把当前 `kb_chunk` 导出到 `data/snapshots/kb_chunk_snapshot.jsonl`。这个目录已在 `.gitignore` 里。每行有 `id`、`doc_id`、`block_kind`、`text`、`image_path`、`page_no`、`bbox`。不建表。
- `--remap` 用同一套排序读快照和重嵌后的块：`doc_id`，`page_no` 空值在后，`bbox` 的 y（没有则按 0），再按 `text`。同一文档里 `(block_kind, text)` 按这个顺序第 n 次出现对第 n 次。改写 `eval/kb_pairs.jsonl` 的 `expected_id`。对不上的题丢掉，并按 `doc_id` 打印丢掉的条数，用来区分「正文对不上」和「这篇重嵌失败、块已经没了」。重复文本的组数也打印。不覆盖 `kb_qa.jsonl`。块 id 是随机的，不能拿来当出现顺序。只在解析阶段失败的文档块 id 没变，映射到自己，题不会丢。

历史会话里旧的 `/kb/images/{旧 chunk_id}` 会 404。不补映射。

---

## 十、Phase G：服务器验收

测试和重嵌都在 `8.134.253.167` 上做。代码目录 `/opt/code/nutri_graph`，Python `/opt/envs/nutri/bin/python`。不要在开发机起库。

pytest 用远程的 `nutri_graph_test`。自动夹具关掉精排。至少覆盖：

- 切块后，图的前后文是相邻正文。`page_no` 有值且 `bbox` 为空的图，两侧为空。
- PDF 插入：图落在纵坐标更小的正文之后；无坐标的图在页末且 `bbox` 为空；像素 bbox 乘 `72/144` 后才参与比较。
- 一张图进 `insert_chunks` 是两条，`image_path` 相同，`block_kind` 分别是 `image` 和 `text`，两条 `dense` 不同。
- 同一 `image_path` 的两条只留高分；`image_path` 为空的两行都留着。
- 精排开关打开且桩掉 HTTP：返回的 `chunk_id` 顺序按桩的名次。桩抛错时，20 条候选只回 `KB_SEARCH_LIMIT` 条，顺序与混合检索一致。没有 API key 时不发请求。
- `_index` 传入不同的写入目录和记录目录时，行里的 `image_path` 不含 `images.new`，字节在写入目录里。两个目录相同时与审核通过的写法一致。
- 重映射：同一文档里两段相同正文按出现次序配对，对不上的题被丢掉并计入该文档的丢弃数。
- `--append`：已有 2 条对照时再追加 1 条，文件里是 3 条。同一 `question` 再追加一次仍然是 3 条。`kb_qa.jsonl` 不被改写。

服务器上的版面解析是 `KB_LAYOUT_BACKEND=dots_mocr`，地址 `http://127.0.0.1:8000/v1`，经 SSH 转到 GPU 机器。坐标换算走的就是这条路径。导入和重嵌都不要改成 `mock`，否则两次评估夹进了解析器差异。开始前先 `curl` 一次 `/v1/models`。隧道不通时 PDF 会 502，先看隧道。

数据顺序：

1. **重嵌前，题集定下来。** 用 `nohup` 跑 `python -m scripts.import_testdata --include-pdf`，把这 4 份 PDF 导入 `eval` 租户：`第一章 Apache Flink 概述.pdf`、`GraphRAG+多模态RAG+Ragas的项目开发.pdf`、`知识库上传的整个流程.pdf`、`食材录入的整个流程.pdf`。已存在的文件名会跳过。这一步在旧代码上做，向量仍是旧的。
2. 不覆盖现有 `kb_pairs.jsonl`。对这 4 篇各执行一次 `python -m scripts.gen_kb_samples --tenant eval --doc-id <该文档 id> --limit 2 --append`。`--limit 20` 按导入时间取最早的块，新 PDF 排在最后，抽不到。`kb_qa.jsonl` 不补，Ragas 不覆盖这 4 篇。
3. `python -m scripts.reembed_kb --snapshot`。部署新代码之后、重嵌之前跑 `python -m eval.kb_run`，记下 Recall。这是「重嵌前」。
4. `python -m scripts.reembed_kb`。每篇日志里有抽图数。不先跑 `--dry-run`。集合名不变。
5. `python -m scripts.reembed_kb --remap`。不整文件重跑 `gen_kb_samples`，不动 `kb_qa.jsonl`。
6. 再跑 `python -m eval.kb_run`。两次题目相同，Recall 可以对比。映射丢掉的题在日志里有条数，不把它们算进前后两次的分母。

抽图张数看重嵌日志，不另跑 `--dry-run`。抽不出图就没有摘要行，这不是精排的问题。拿一份真实 PDF 看图是否落在相邻正文之间，不要只靠单测里的假坐标。

---

## 十一、验收

- 一篇带图的 Markdown 或 DOCX 通过后，该图在 `kb_chunk` 里有两行，`text` 不超过 300 字，两侧正文进过描述提示词。
- 文本提问能召回这张图，结果里同一路径只出现一次。图片提问同样只出现一次。
- 文本提问能在调用追踪里看到 `kb_rerank`，日志里有精排分和 `usage`。同一道题开关前后的 `chunk_id` 顺序可以相同，融合结果本来就对时精排不会改顺序。顺序相同算通过。精排接口失败时仍返回答案，条数是 `KB_SEARCH_LIMIT`。
- 无坐标的 PDF 图描述里没有编造的前后文，只有章节路径。
- 已通过文档重嵌后，旧 `chunk_id` 不在 `kb_chunk` 里，新行能被提问命中，源文件还在。用新的图片块 id 请求 `/kb/images/{chunk_id}?tenant_id=`，返回 200，内容是这张图。文本能被搜到而图片 404，算重嵌失败。
