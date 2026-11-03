## 一、需求简述
```
1.1 参考/Users/long/coder/code/msb/ai/gme_rag/Multimodal_RAG 项目对图片向量化的处理：
   图片+图片的前后文档->丢该大模型生成不超过300字的摘要，提示词也可以参考 Multimodal_RAG 项目，然后将图片地址、图片向量、图片摘要、图片摘要向量保存到向量库；
1.2 数据召回之后再做一次精排，模型使用 RERANK_MODEL_POOL 变量；
```


## 二、需求详述

> 性质：这是**需求文档**（对照新简述、参考项目和现有代码后的结论），不是实施方案。方案留给 plan_v13。
> 范围：只动企业知识库的图片入库和问答召回。食疗问答、实体链接、长期记忆、联网检索不在本需求里。
> 第三节、第四节里要拍板的都已定。第四节的施工口径除「重嵌收窄到只换图片行」之外都成立，已收进本节。重嵌仍是全量。

### 2.0 结论

新简述能落地，而且不推翻 needs_v10 的独立向量。

1. **摘要一次生成。** 图片加上同文档前后正文，用 `KB_CAPTION_MODEL` 写不超过 300 字的描述。提示词沿用参考项目 `generate_image_description` 的三档（前后都有、只有一侧、两侧都没有）。
2. **一张图存两条独立向量，仍用现在这张表。** 现有集合一行只有一个 `dense`，放不下「图片向量 + 摘要向量」。同一张图写两行，都进 `kb_mm_tongyi_vision_plus_1152`（1152 维，`COSINE`）：一行的 `dense` 是纯图片向量，另一行的 `dense` 是摘要的纯文本向量。图片地址两边都写 `image_path`，摘要两边都写 `text`。不调用融合向量，不换 gme_rag 的 1024 维 / `IP` 那套字段。
3. **文本召回之后精排。** 模型取 `RERANK_MODEL_POOL` 的第一项（`qwen3.7-text-rerank`）。只作用于企业知识库的文本提问。

摘要向量必须走现在的 `embed_mm.embed_text`，和图片向量同一个多模态模型。换到食疗线的 `text-embedding-v3`（1024 维）就写不进这个集合。

### 2.1 和参考项目的差别

参考项目的描述步骤可以直接用。它的入库步骤不能照搬：`process_item_with_guard` 把图片和描述打成一条融合向量，整张图在 `t_doc_collection` 里只有一个 `dense`。新简述要的是地址、图片向量、摘要、摘要向量四样都在，所以是两条独立向量，不是那一条融合向量。

### 2.2 一张图的两行

| | 图片行 | 摘要行 |
|---|---|---|
| `block_kind` | `image` | `text` |
| `text` | 不超过 300 字的摘要 | 同一段摘要 |
| `image_path` | 本地路径 | 同一路径，回答时仍能带出这张图 |
| `dense` | `embed_image_bytes` | `embed_text(摘要)` |
| `sparse` | 由摘要自动生成 | 由同一段摘要自动生成 |

正文块保持今天的一行：`dense` 只嵌正文。检索不用改字段。文本提问继续走 `dense` + BM25，会同时碰到摘要向量和图片向量。图片提问继续只嵌上传的图，走 `dense`。

两行文本相同，文本提问和图片提问都会把同一张图捞成两条。去重放在检索返回处，文本和图片共用：按 `image_path` 分组，留分高的一条；`image_path` 为空的正文不参与合并。离线 Recall 若标准块被兄弟行挤掉，同一 `image_path` 算命中。

### 2.3 精排

只加在企业知识库问答的文本提问上，放在问答链路里，插在混合检索和生成之间。不放进 `search_question`：离线评估也走这个检索函数，放进去会把评估的行为和费用一起改掉。

- 模型名用 `RERANK_MODEL_POOL` 的第一项。走百炼文本精排的原生接口。现有的 `compatible-mode` 地址没有这个接口。
- 混合检索先留 20 条，精排按「问题 × 候选的 `text`」打分，再截成 `KB_SEARCH_LIMIT`（默认 6）。
- 清单为空，或调用失败，按混合检索原来的顺序截回这 6 条。不能把 20 条都送给生成。
- 纯图片提问没有查询文本，不精排。
- 精排分只用来排序。它和向量余弦不是同一个量纲，不拿去跟检索阈值比。

### 2.4 前后文和 PDF

前后文在**切块之后、写成两行之前**取。每个图片块向前、向后各找最近一个 `block_kind=text` 的块，跳过其它图片，各取一块。判断依据是 `block_kind`。这时候图片路径还没写上，不能靠路径空不空来认正文。前后文各截到约一块的长度。摘要写进库和拿去嵌入的都是截断后的 300 字。

PDF 只把图插进现有正文顺序，不把整页按纵坐标重排。图插到「本页最后一条纵坐标小于它的正文」后面，再取前后文。插完按新顺序重算章节路径。Markdown、DOCX 本来就夹在段落里，顺序不动。

`dots_mocr` 给的是 144dpi 渲染图上的像素，PyMuPDF 给的是 PDF 点。同一页合并前先把像素换算成点，否则图会插到页首或页尾。没有坐标的图仍放在页末，描述只带章节路径。

### 2.5 已通过文档：全量重嵌

演示库里已通过的文档整篇重嵌，不在页面上逐条点通过，也不收窄成只换图片行。每个文档先按 `doc_id` 删掉 Milvus 旧行和 `kb_chunk`，再按新规则整篇解析写入。集合名、维度不变。

这条不走现有的「通过」接口。文档已经是通过状态时，那个接口会直接返回，不会重写向量。重嵌失败要把状态打回待审核并记下原因，不能留着「已通过但库里没有向量」。

全量重嵌会换掉全部 `chunk_id`。连带三件事一起做：

- 重嵌后重新生成离线评估的对照样本，否则下一次评估会因找不到旧块而退出。
- 历史会话里保存的图片地址会失效，不做旧链接兼容。
- 只删该文档图片目录里的旧图。源文件留在文档目录里。

现有库图片很少，看不出摘要向量和去重的差别。把 `data/testdata` 里 4 份带图 PDF 导入 `eval` 租户，重嵌前后各跑一次离线评估，留下对比。

## 三、需求补充

> 下面两项已拍板，施工口径以 2.4 为准。

### 3.1 PDF 的前后文是错位的

Markdown、DOCX 的图夹在段落之间，按切块顺序取前后正文是成立的。

PDF 默认把一页的图都放在该页正文后面。图的「前文」会变成该页最后一行，「后文」变成下一页开头。`dots_mocr` 路径把图整篇追加到文末，前后文会绑到文档尾巴上。

**建议**：本期把 PDF 的图按页、按纵坐标插回正文之间，再取前后文。没有坐标的图只带章节路径。

按照建议。

**已拍板**：按建议做。无坐标的图只带章节路径。

### 3.2 已通过文档要重刷

摘要文本、图片向量旁边的摘要向量都是新的。只改代码，已通过的文档检索结果不变。集合名和维度不变，重刷是重写行，不是换集合。

**建议**：演示库里已通过的文档重嵌。重刷时先按 `doc_id` 删掉旧行再写入。

按照建议。

**已拍板**：单独重嵌，不逐条重新点通过。先按 `doc_id` 删除旧行再写入。


## 四、需求评审

> 对照本文档第一～三节（含 2.4 已拍板口径）+ 参考项目 `Multimodal_RAG` + 现有代码。
> 已读：`config.py`、`kb/caption.py`、`kb/chunk.py`、`kb/embed_mm.py`、`kb/milvus.py`、`kb/publish.py`、`kb/store.py`、`kb/parse.py`、`kb/parse_pdf.py`、`kb/parse_md.py`、`kb/parse_docx.py`、`kb/search.py`、`kb/workflow.py`、`kb/memory.py`、`api/kb.py`、`eval/kb_run.py`、`migrations/029_kb.sql`、`rag/client.py`、`.env`；
> 参考项目读了 `milvus_db/db_operator.py`（`generate_image_description`/`process_item_with_guard`）与 `utils/common_utils.py`（`get_surrounding_text_content`）。
> 顺带查了线上库现状（2026-11-03）：`kb_document` 已通过 13 篇（`default_renter` 2 + `eval` 11），`kb_chunk` 283 块，其中**图片块只有 3 个**（`02-Nginx安装.pdf` 2 张、`包车智能体流程.pdf` 1 张；11 篇 md 全 0 图）。
> 未改代码。日期：2026-11-03。

### 4.1 结论

需求成立，可以写 plan_v13。三条主线（摘要带前后文、一图两行、文本精排）都与现有代码兼容：**不换集合名、不改 1152 维、不加 Milvus 字段**，现有 283 块里的正文块一行都不用动。

但有五条口径必须在 plan 里钉死，否则实现会走偏（下面 4.2 是逐条落点）：

1. **前后文要在「切块后」取，不在「解析后」取**，且只取紧邻一块。解析块可能是整段几千字，取它会让 prompt 和缓存都失控。
2. **PDF 坐标重排有两套坐标系**：`_from_pymupdf` 的 bbox 是 pt，`dots_mocr` 的 bbox 是 dpi=144 渲染图上的像素，差 2 倍。同页合并前不归一，图会插到错位。
3. **按 `image_path` 去重必须放进检索层，且图片提问也要去重**。2.2 只写了文本提问；但多模态嵌入的图向量与摘要文本向量在同一空间，图片提问同样会把同一张图捞成两条。
4. **精排要放在 `kb/workflow.py`，不要塞进 `milvus.search_text` / `search_question`**。`eval/kb_run.py::_recall` 用的是 `search_question`，塞进去等于离线评估被连带改行为、每跑一次多花 17 次精排钱。
5. **3.2「按 doc_id 全量重嵌」会让全部 `chunk_id` 变化**，17 条 `kb_pairs.jsonl` 立即报废、历史会话里落库的 `image_url` 全部 404。这一条建议收窄范围，见 4.2.7（需你确认是否改口径）。

另外要先认清本期的数据现实：**283 块里只有 3 张图**。摘要向量、两行去重、图片精排的效果在现有演示库上基本测不出来——不补样本的话，这一期只能是「代码对了，效果看不见」。

### 4.2 施工口径（写 plan 时照此执行）

**4.2.1 摘要（`kb/caption.py`）**

- 签名扩成 `caption_bytes(raw, *, mime, heading, filename, prev_text="", next_text="")`，三档提示词照参考项目 `db_operator.py:120-145`：前后都有 / 只有一侧 / 两侧都没有，末尾统一「描述内容长度最好不超过300个汉字」。
- **截断从 `text[:800]` 改成 `text[:300]`**。现在 800 是历史值，与「不超过 300 字」冲突；存进 `text` 和拿去 `embed_text` 的必须是同一段。
- 前后文各截 500 字（≈一个块，`KB_CHUNK_CHARS=480`），避免 PDF 长段落把 prompt 撑大。
- 模块 docstring 第一句「描述不进稠密向量」已过时，改掉。
- 空描述兜底 `图:{filename}` 保留（摘要行仍要写一行，别丢图）。

**4.2.2 前后文在哪取（`kb/chunk.py`）**

- 在 `prepare()` 收尾对 `out` 做第二遍遍历，给每个 `block_kind == "image"` 的块补 `prev_text` / `next_text`。
- **判断「是不是正文」只能用 `block_kind`，不能用 `image_path` 判空**：图片块的 `image_path` 是 `publish._index` 阶段才写的，此刻是空串；照抄参考项目 `get_surrounding_text_content` 的 `image_path is None` 会把图片自己当正文。
- 取法同参考项目：向前/向后找最近的 `text` 块，跳过其他 `image` 块，各取一块。
- 不跨文档、不跨 doc_dir；首块图无前文、末块图无后文，走三档提示词的单侧分支。

**4.2.3 PDF 重排（`kb/parse_pdf.py`）**

- `_from_pymupdf`：正文行保持原有顺序，**只做「插入」不做整页重排**——图片按 `bbox[1]` 找到「最后一条 y0 小于它的正文行」，插到该行之后。整页按 y 全排序会把双栏 PDF 的左右栏揉在一起。
- 插完顺序变了，`heading_path` 要**按新顺序重算**。现在的写法是图片统一拿该页循环结束时 `heading`（该页最后一个标题），图在页中间时章节路径就是错的。
- `_image_bbox` 拿不到坐标（`get_image_info` 的 xref 对不上，常见于嵌套 XObject）→ 保持页末追加 + 不编前后文，符合 2.4。
- `dots_mocr` 路径：`_blocks_from_layout_json` 的 bbox 来自模型对 144dpi 渲染图的识别结果（像素），`_from_pymupdf` 的 bbox 是 PDF 点。**同页合并前必须乘 `72/144` 归一**，否则图全被插到页首或页尾。这是本期最容易踩的一个坑。

**4.2.4 一图两行的写入（`kb/publish.py` + `kb/milvus.py`）**

- 图片块产出两条 row：
  - 图片行：`chunk_id = <图片 uuid>`（图片文件按它命名，`/kb/images/{chunk_id}` 取图靠它）、`block_kind=image`、`dense = embed_image_bytes`；
  - 摘要行：`chunk_id = <新 uuid>`、`block_kind=text`、`dense = embed_text(摘要)`、`image_path` 指向同一文件、`image_md5` 同值。
- 两行的 `text` 都写摘要（图片行也写），这样图片行的 BM25 也从摘要生成，与 2.2 表格一致。
- `kb_chunk` 两行都插：`id` 是主键，不冲突；`/kb/images/{chunk_id}` 两条都能取到图，因为接口是走 `image_path` 而不是 `block_kind`。
- **不新增字段是对的**：Milvus 加向量字段要重建集合，两条独立行是本期唯一不重建的走法。plan 里写明这个取舍，免得后面有人想「一行为什么不塞两个 dense」。

**4.2.5 去重（`kb/search.py`）**

- 放在 `search_question` 和 `search_picture` 的返回处，按 `image_path` 分组，同组留 `score` 高的；`image_path` 为空的行不参与分组。
- 放检索层而不是 `workflow`：离线评估也走 `search_question`，只有同一处去重，线上和评估才是一个口径。
- `eval/kb_run.py::_recall` 要同步改：预期块被兄弟行挤掉时按 `image_path` 判命中，否则 R@k 会假跌（`expected_id` 可能是被挤掉的那一条）。

**4.2.6 精排（新增 `kb/rerank.py` + `kb/workflow.py`）**

- **走原生端点**：`https://dashscope.aliyuncs.com/api/v1/services/rerank/text-rerank/text-rerank`，body `{"model":..,"input":{"query":..,"documents":[..]},"parameters":{"return_documents":false,"top_n":N}}`，取 `output.results[].index/relevance_score`。
- `config.DASHSCOPE_BASE_URL` 现在是 `compatible-mode/v1`，**`/compatible-mode/v1/rerank` 是 404**（已实测），不能复用 `ChatOpenAI`/`OpenAI` 客户端，也不能只改 base_url。新增 `RERANK_BASE_URL`（默认上面那个原生地址）。
- 模型名取 `RERANK_MODEL_POOL[0]`；清单为空或名字为空 → 直接跳过，不报错。
- 插在 `workflow._retrieve` 尾部（或单独加一个 `rerank` 节点，在 `retrieve` 与 `generate` 之间）。**不要改 `milvus.search_text` 的语义**，那里只负责「召回 20 条」：把 `hybrid_search` 的 `limit` 从 `limit` 改成 `KB_RERANK_CANDIDATES`（默认 20）。
- 送精排的 `text` 截 500 字（`qwen3.7-text-rerank` 序列上限会截断，超长反而影响打分）。
- **降级必须截到 `KB_SEARCH_LIMIT`**：调用失败/超时/返回空 → 按混合检索顺序取前 6 条，不能把 20 条全塞给生成（上下文暴涨、答案变散）。
- 纯图片提问跳过（没有 query 文本），保持 `search_picture` 原样。
- 精排后 `score` 是 `relevance_score`，与融合分不同量纲。前端不展示分数（已核对 kb-web 无 score 渲染），不影响 UI；但 `observe`/日志里要标明分数来源，别和 `CHUNK_THRESHOLD=0.40` 那套比。
- 新增配置：`KB_RERANK_ENABLED`（默认 true）、`KB_RERANK_CANDIDATES`（默认 20）、`RERANK_BASE_URL`；`.env.example` / `README.md` 同步。
- 用 `span("node", "kb_rerank")` 包一层，调用追踪页能看到耗时。

**4.2.7 重嵌（新增 `scripts/reembed_kb.py` + `publish.reindex`）**

下面的 B 是评审时的建议，**没有采纳**。已定走 A，口径在 2.5。

先给两条路，差别很大：

| 方案 | 做什么 | 成本（当前 13 篇） | 副作用 |
|---|---|---|---|
| A 全量重嵌（3.2 现口径） | 按 `doc_id` 删 Milvus 行 + 全部 `kb_chunk`，整篇重新解析入库 | 283 块 embed + 逐句语义切分 embed（`chunk.py::_embed_sentences` 是逐句调用，量远大于 283）+ 3 次 caption | 全部 `chunk_id` 变 → 17 条 pairs 报废、历史 `image_url` 全 404 |
| B 只换图片行（建议） | 只删 `block_kind='image'` 的 kb_chunk 行 + Milvus 里同一条件，按新规则重写这 3 张图的 2 行 | 3 次 caption + 6 次 embed | 正文块 `chunk_id` 不变 → 评估样本、历史图片链接都不受影响 |

**建议走 B。** 理由：正文块的 `dense` 只嵌正文，本期没有任何改动碰到它；3.1 的坐标重排只影响「图插在哪、前后文是谁」，不改正文内容与切分结果。3.2 想要的结果（摘要、摘要向量都是新的）B 也能达到，代价却小两个数量级。



如果坚持 A，下面三条必须一起做：

- 重嵌后**同批重跑 `scripts/gen_kb_samples.py`** 重新生成 `kb_pairs.jsonl` / `kb_qa.jsonl`，否则下一次 `eval.kb_run` 会在 `_tenant()` 抛「找不到块」直接退出。
- 接受历史会话图片 404：`api/kb.py:92/180` 把带 `image_url` 的 hits 落库（`memory.remember`），`chunk_id` 一变老链接就全废。演示期可接受，但 plan 里写明「不做兼容」。
- 删旧图目录：重嵌会给图新 `chunk_id` 新文件名，`doc_dir/images/` 里旧图会残留。**只删 `images/` 子目录，不能动 doc_dir 本身**（`source.*` 在那儿）。

两种方案都要注意：

- `publish` 里加 `reindex(doc_id)`，**不要复用 `approve()`**——`approve()` 对 `status=approved` 是直接早退，什么都不做。
- 失败要把 `status` 打回 `pending` + 写 `error`（同 `approve()` 的异常分支），不能留「没有向量但状态 approved」。
- 给 `--dry-run`：打印每篇解析出多少图、预计 caption/embed 调用次数。本期 3 张图，dry-run 一眼就能看出「PDF 图片抽取覆盖率」这个更大的问题（见 4.5）。

### 4.3 需求覆盖核对

| 需求 | 落点 | 判定 |
|---|---|---|
| 1.1 图片 + 前后文 → 不超过 300 字摘要 | `caption` 三档提示词 + `chunk.prepare` 提供前后文 | 成立（须改签名、截断 800→300） |
| 1.1 图片地址、图片向量、图片摘要、摘要向量都进向量库 | 一图两行，`image_path` / `text` / `dense` 已够用 | 成立（不加字段、不换集合） |
| 1.2 召回后再精排，模型用 `RERANK_MODEL_POOL` | 新增 `kb/rerank.py` 原生端点 + `workflow` 节点 | 成立（须新写 HTTP 客户端，SDK/OpenAI 兼容口都不通） |
| 2.2 正文块保持今天的一行 | 不动 `publish._index` 的 text 分支 | 成立 |
| 2.2 文本提问同时碰到摘要向量与图片向量 | 两行都进同一 `dense` 字段 | 成立，靠 `image_path` 去重收口 |
| 2.2 图片提问只嵌上传图 | `search_picture` 不动 | 成立，但**同样要去重**（需求未写，建议补进 2.2） |
| 2.4 PDF 按页/纵坐标插回、无坐标只带章节路径 | `parse_pdf` 插入式重排 + `_image_bbox` 空值分支 | 成立（注意坐标系归一与 heading 重算） |
| 2.4 已通过文档重嵌、先按 doc_id 删 | `publish.reindex` + 脚本 | 成立，但建议收窄到图片行（4.2.7） |

### 4.4 范围确认

| 项 | 判定 |
|---|---|
| 图片摘要改带前后文 + 300 字、摘要向量、一图两行、文本精排、PDF 坐标插入、已通过文档重嵌 | 做 |
| 换集合/加 `dense_summary` 字段/换 1024 维（gme_rag 那套）、`process_item_with_guard` 式融合向量、食疗线（`graph/nodes.py::vector_fallback_node`）精排、图片提问的 query 改写、跨文档取前后文、摘要结果缓存复用 | 不做 |

### 4.5 风险与注意

- **本期数据看不见效果（最该说的一条）**：283 块只有 3 张图，且都在 2 篇 PDF 里；11 篇 md 一张图都没有。摘要向量是否带来召回提升、两行去重是否真的省了一次重复、图片精排是否有效，用现有库都验不出来。建议本期顺手用 `data/testdata` 里 4 份带图 PDF（Flink 概述 / GraphRAG 多模态 / 两份 SOP）在 `eval` 租户做一次对照：重嵌前后各跑一遍 `eval.kb_run`，至少让 Rec@K 有个对比数。
- **PDF 抽图覆盖率本身就是瓶颈**：`02-Nginx安装.pdf` 只抽出 2 张图、`包车智能体流程.pdf` 只有 1 张，很可能 `page.get_images` 对内嵌/矢量图无效。如果抽不出图，摘要、两条向量都无从谈起。建议 dry-run 里把「每篇抽出图数」列出来，先确认抽取不是瓶颈再谈效果。
- **重嵌的成本不在 caption**：`chunk.py::_embed_sentences` 是逐句调 `embed_text`，一篇长文档可能几十次调用。走 A 方案时脚本要按 doc 提交、能断点续跑，别一个事务跑 13 篇。
- **评估历史不可比**：去重 + 重嵌后 `kb_eval_run` 里旧的 Recall 与新口径不可比（`R@1=76.5% / MRR=0.847` 那组是 283 块旧图、无去重的结果），前后对比要在文档里注明口径。
- **精排延迟**：单次 +200～500ms，`/kb/ask` 与 `/kb/ask/stream` 首字变慢；流式页面「正在查找」会多停一拍，属预期，不要当卡死。
- **精排的分数不能与阈值混用**：`relevance_score` 是 0～1 的排序分，不是 cosine，别接到 `CHUNK_THRESHOLD` 或前端「相关度」上。
- **两行会让 `kb_chunk` 的块数统计翻倍**（图片块 ×2），管理后台文档详情、`gen_kb_samples.py` 的抽样、`eval` 的块数都会变；这些地方不读 `block_kind` 的话要顺带确认。
- **测试要补的断言**：`tests/test_kb_publish.py::quiet_index` 已经把 `milvus.delete_doc` / `insert_chunks` / `embed_mm` / `caption` 全打了桩，本期只需在桩上断言「一张图进 `insert_chunks` 是 2 条、`image_path` 相同、`block_kind` 分别是 image/text、两条 `dense` 不同」。再补两组：`search.py` 的 `image_path` 去重（纯函数，好测）、`rerank` 的降级（打桩抛异常，断言只回 `KB_SEARCH_LIMIT` 条）。测试照旧上远程 `nutri_graph_test` 跑，别在本机。

### 4.6 拍板

1. **3.2 重嵌范围**：走 A（全量重嵌，现口径，chunk_id 全变、样本要重跑）还是 B（只换图片行，chunk_id 不变，成本 1/50）？**建议 B。**

还是走 A 方案，现在都是测试数据，处理简单。

**已拍板**：全量重嵌。副作用按 2.5 接受，不改成只换图片行。

2. **图片提问要不要一起去重**：2.2 只写了文本提问去重，但一图两行后图片提问同样会重复命中同一张图。**建议一起做**（同一个函数，不加成本）。

按照建议。

**已拍板**：文本提问和图片提问都按 `image_path` 去重。

3. **本期要不要补带图样本做对照**：现有库只有 3 张图，效果验不出来。**建议**把 `data/testdata` 的 4 份带图 PDF 导进 `eval` 租户，重嵌前后各跑一次 `eval.kb_run` 留个对比数。

按照建议。

**已拍板**：导入这 4 份 PDF，重嵌前后各跑一次离线评估。
