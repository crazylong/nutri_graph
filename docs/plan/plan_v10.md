# plan_v10 —— 企业知识库（图文）施工计划

> 生成时间：2026-11-01
> 上游：[`docs/needs/needs_v10.md`](../needs/needs_v10.md)。第二节是需求，第三节是评审口径。
> 与第二节原文冲突时，按第三节 3.3 施工。这四处已经写进下面正文，不要再回 needs 里抄：
> 混合检索按本仓库的 `MilvusClient` 新写 schema；Recall@K 对本库结果现算；租户手填、下拉读已有值；问答页是独立 Vue 应用。
> 2026-11-01 又按对 `review_v10` 的复核改了检索向量、图片大小、trace 树和两处引用。复核结论以本节正文为准。
> 性质：只写怎么施工。食疗问答链路、`kb_chunks`、`kg_entity` 不动。

---

## 一、范围

| Phase | 内容 | 大致人日 |
|---|---|---|
| A | 文档表、块表、评估结果表；新 Milvus 集合；多模态嵌入 | 1.5 |
| B | md / pdf / docx 解析；审核通过才写入向量 | 2.5 |
| C | 问答接口、独立短链路、trace | 1.5 |
| D | 后台「知识库查询」「知识库审核」 | 1.5 |
| E | 独立 Vue 问答页、nginx | 1.5 |
| F | 离线 Ragas + 双向 Recall@K，结果挂在查询页 | 1.0 |
| **合计（不含验证）** | | **≈ 9.5 人日** |
| 服务器上跑测试、迁移、两套前端构建 | | 1.0 |

**顺序：A → B →（C 与 D 并行）→ E → F。** C 依赖 B 的可检索数据。D 的通过按钮依赖 B。E 依赖 C 的接口。F 依赖 C 的问答函数。

---

## 二、本期不做

Reranker、父子块检索（小块召回、父块生成）、会话记忆、按 `bbox` 裁图、表格 / 公式专项、vLLM、角色 / 菜单权限、租户表、租户管理页、第三套 trace、在线评估降级、CLIP_benchmark / open_clip / NL-DIR 依赖、食疗自定义指标打知识库的分。

2.6 的三级切分只落到「块上带标题链」。限流和并行解析不单独立项。解析或嵌入失败保持 `pending`。

---

## 三、要动的现有文件

| 位置 | 改什么 |
|---|---|
| `rag/client.py:27` `_get_client` | 改名为 `get_client` 并给知识库用。`insert_chunk` / `search_chunks` 仍只服务 `kb_chunks` |
| `app.py:119-122` | 挂 `api/kb.py`、`api/admin_kb.py` |
| `config.py:36-37` 附近 | 新集合名、多模态嵌入模型名（`tongyi-embedding-vision-plus-2026-03-06`）、描述模型名。不改 `EMBEDDING_MODEL` 默认值 |
| `.env.example:26`、`:39`、`:104` 附近 | 补知识库变量。`TRACE_ENABLED` 的代码默认值保持 `false` |
| `admin-web/src/views/Layout.vue:8-15` | 侧栏加两项。食疗菜单不动 |
| `admin-web/src/router.js:24-38` | 两个子路由 |
| `deploy/nginx/nutri-graph-locations.conf` | `/kb/` 反代，`/ask/` 静态页。写在 `location /` 之前。旧地址 `/kb-ask/` 301 到 `/ask/` |
| `requirements.txt` | 加 `pymupdf`、`python-docx`、`dashscope`（百炼多模态嵌入 SDK，项目已在用）。`dots.mocr` 走独立解析服务。不加 CLIP、不加 vLLM |

新增目录：`kb/`（解析、索引、问答）、`api/kb.py`、`api/admin_kb.py`、`kb-web/`（Vue）、`migrations/029_kb.sql`、`eval/kb_run.py`、`tests/test_kb_*.py`。

`graph/workflow.py`、`graph/nodes.py`、`eval/metrics.py` 的食疗指标、`frontend/` 不改。

---

## 四、Phase A：表和集合

### A1 · PostgreSQL

新增 `migrations/029_kb.sql`。文档在 PG，向量只是派生。

```sql
CREATE TABLE kb_document (
    id                  UUID PRIMARY KEY,
    tenant_id           TEXT NOT NULL,
    title               TEXT NOT NULL,
    doc_kind            TEXT NOT NULL,          -- tech | policy
    status              TEXT NOT NULL,          -- pending | approved | rejected
    filename            TEXT NOT NULL,
    storage_path        TEXT NOT NULL,
    uploader_user_id    TEXT NOT NULL,
    uploader_phone_mask TEXT NOT NULL,
    error               TEXT NOT NULL DEFAULT '',
    created_at          TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at          TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE TABLE kb_chunk (
    id           TEXT PRIMARY KEY,             -- 审核通过时生成，标答认这个 id
    doc_id       UUID NOT NULL REFERENCES kb_document(id) ON DELETE CASCADE,
    tenant_id    TEXT NOT NULL,
    block_kind   TEXT NOT NULL,                -- text | image
    text         TEXT NOT NULL,                -- 正文，或图片的上下文描述
    page_no      INT,
    bbox         JSONB,
    heading_path TEXT NOT NULL DEFAULT '',
    image_path   TEXT,
    image_md5    TEXT
);

CREATE TABLE kb_eval_run (
    id         UUID PRIMARY KEY,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    ragas      JSONB NOT NULL,
    recall     JSONB NOT NULL
);
```

`tenant_id` 只出现在这三张表和新集合上。不改食疗表。

原文件放 `data/kb/{doc_id}/source`，抽出的图放 `data/kb/{doc_id}/images/{chunk_id}`。删除文档时删整个目录。

### A2 · Milvus 新集合

集合名默认 `kb_mm_tongyi_vision_plus_1152`（`config.KB_COLLECTION`）。只用字母、数字、下划线。

`kb/milvus.py` 用显式 schema，不用 `rag/client.py` 的 `_ensure_collection`（那个只有一个稠密向量）。

| 字段 | 用途 |
|---|---|
| `chunk_id` | 与 `kb_chunk.id` 相同，删除和标答都认它 |
| `doc_id` | 按文档删除 |
| `tenant_id` | 检索过滤 |
| `block_kind` | `text` / `image` |
| `text` | BM25 的输入。图片块写上下文描述，不留空 |
| `heading_path` | 回给问答页 |
| `image_path` | 文本块为空串 |
| `dense` | 1152 维，`COSINE`（tongyi-embedding-vision-plus 默认维度） |
| `sparse` | BM25 Function，输入 `text`，`SPARSE_INVERTED_INDEX` |

插入时不填 `sparse`，由 Milvus 从 `text` 生成。集合已存在时核对 `dense` 维度，不一致就拒绝写入并打日志，不往旧集合里混另一种向量。

调用用 `pymilvus>=3.0.2` 的 `MilvusClient`：`AnnSearchRequest` + `WeightedRanker` + `hybrid_search`。参考项目锁的是 `pymilvus==2.6.0`，`db_retriever.py` 不搬。

### A3 · 嵌入

新文件 `kb/embed_mm.py`，用 **`tongyi-embedding-vision-plus-2026-03-06`** 出 1152 维向量，**走百炼 `dashscope.MultiModalEmbedding.call`**（复用现有百炼 key，数据不出境）。

检索只用**独立向量**。百炼把独立向量定义为以文搜图、以图搜文；融合向量是另一种返回类型（`fused`），文档没有保证它能和纯文本向量比。验收要的是「文字问到图」和「用图问到文字块」，两条都落在独立向量上。

- 文本块、文本提问：content 里只有 `text`。
- 图片块、图片提问：content 里只有 `image`（URL 或 base64）。
- 一次 content 里同时放 `text` 和 `image` 会得到融合向量。本计划不这么调用，也不把两种向量写进同一个 `dense`。
- 图片的上下文描述仍然生成，写入 Milvus 的 `text` 字段，给 BM25 和回答用。描述不进稠密向量。
- 模型名仍用带日期的快照。不调用 `rag/embed.py` 的 `get_embeddings()`。那条是 `text-embedding-v3`，还带食疗缓存。

凭证复用现有 `DASHSCOPE_API_KEY`，不新增 key。

描述图用单独配置 `KB_CAPTION_MODEL`（默认 `qwen-vl-plus`），直接调 DashScope 多模态对话。不要放进 `LLM_MODEL_POOL`：池的注释写明视觉模型协议不兼容（`config.py:104-105`）。

**A3 的前置 smoke test（写代码前先做，一条脚本）**。独立向量路能不能成立，取决于百炼对**中文文档图（架构图/流程图，带中文标注）**的文本↔图对齐质量，这个写文档定不死。所以 `kb/embed_mm.py` 动工前，先跑一条一次性脚本验证：

- 拿 1~2 张真实架构图/流程图（演示要用的那种，带中文标注），先 `image` 独立向量入库，再用 1~2 句中文问句走 `text` 独立向量检索。
- 判定：问句能命中对应那张图（前 3 名里出现），则独立向量路成立，继续铺开。
- 若命中不了：先别往下写，回来重新核对 `MultiModalEmbedding` 的独立向量用法（是不是 content 结构、维度、返回字段取错了），或考虑退回「图 + 描述融合 + 提问先 caption」的备选。**这个判定要在写 B 解析器和 C 检索之前做完，否则 Phase E 的「用图提问」验收可能整条返工。**

脚本放 `kb/_smoke_embed.py`（一次性，不入 tests），跑完记录结果后可以留着当调试工具，不进交付清单。

---

## 五、Phase B：解析和审核入库

### B1 · 三种解析器

统一成块：`text`、`block_kind`（`text` | `image`）、`page_no`、`bbox`、`heading_path`。后两个允许为空。一篇知识一个文件。

| 格式 | 实现 | 说明 |
|---|---|---|
| Markdown | `kb/parse_md.py`，按 ATX 标题切，抽出 `![]()` | 图若是外链或文件里没有，这块记为空描述，不为此改成 zip 上传 |
| PDF | `kb/parse_pdf.py`，PyMuPDF 抽图 + **dots.mocr** 版面解析（`KB_LAYOUT_BACKEND`） | dots.mocr 给标题层级、版面坐标、阅读顺序，PyMuPDF 抽图和页码/bbox。版面解析走独立服务（参考项目的 dots.ocr 升级为 dots.mocr） |
| DOCX | `kb/parse_docx.py`，python-docx | 认 Heading 样式做标题链；抽段落和图。`page_no`、`bbox` 留空 |

**版面解析走 Provider 抽象（关键：没 GPU 也能先跑通）**。dots.mocr **没有官方在线 API，只能私有化部署，且必须 GPU**（BF16 约 7.4GB 显存，最小 8GB 卡，无实用 CPU 路径）。现有服务器无 GPU，所以解析器做成可切换的 Provider，`KB_LAYOUT_BACKEND` 取值：

- `mock`（默认，先跑通全链路）：返回**结构真实**的假解析结果——有标题层级、有页码、有 bbox、能抽出图，够下游切块/嵌入/检索/问答/评估全流程先验收。等 GPU 就位再切。
- `dots_mocr`：调 vLLM 起的 dots.mocr 端点，走 **OpenAI 兼容** `/v1/chat/completions`。dots.mocr 是图像 OCR 模型，PDF 先由 PyMuPDF 按页渲染成 PNG（dpi=144）再 base64 逐页喂给它，prompt 用官方 `prompt_layout_all_en`（输出带 bbox、类别、文字的 JSON）。配置 `KB_LAYOUT_URL`（含 `/v1`）、`KB_LAYOUT_MODEL`、`KB_LAYOUT_API_KEY`（可空）。见 `docs/deploy.md`。

切换只改 `KB_LAYOUT_BACKEND`，解析这一环换了实现，切块/嵌入/检索/问答/评估零改动。

长节再用已有的 `RecursiveCharacterTextSplitter` 切到 480 字、重叠 48（`KB_CHUNK_CHARS` / `KB_CHUNK_OVERLAP`）。这个模型文本上限 1024 token，中文按一字一 token 估，800 字可能超限，所以留余量。稠密向量按这 480 字嵌入；Milvus 的 `text` 字段截到 4000 字，BM25 保留更长原文。切出来的块各自入库，生成时用这些块，不另取父块。标题缺失则沿用上一块的 `heading_path`。同一篇里图片 MD5 相同只留一块。这个模型单张建议不超过 5MB，硬上限 10MB。超过 10MB：整篇不通过，`error` 写明原因。5MB 到 10MB 仍嵌入，`error` 里记一笔高于建议值。不把超限的图悄悄丢掉再让其余正文通过，否则演示用的架构图会从检索里消失。

### B2 · 状态和向量一起变

逻辑放 `kb/publish.py`。接口返回时，检索结果已经是新状态。

| 动作 | 状态 | 向量和 `kb_chunk` |
|---|---|---|
| 添加 | `pending` | 不写。文件先落盘 |
| 编辑 | `pending` | 按 `doc_id` 删掉 Milvus 和 `kb_chunk`。换了文件就覆盖原文件 |
| 通过 | 成功才 `approved` | 解析 → 描述图 → 独立向量嵌入 → 写入。任一步失败：删掉本次已写入的向量，保持 `pending`，`error` 记下原因 |
| 驳回 | `rejected` | 删掉该文档的块 |
| 删除 | 行删除 | 删向量、删 `data/kb/{doc_id}/` |

添加时 `tenant_id`、标题、`doc_kind`、文件都必填。`tenant_id` 只允许 `^[A-Za-z0-9_]{1,32}$`，避免拼进 Milvus 过滤表达式。`doc_kind` 只允许 `tech`、`policy`。上传人取 `require_admin`（`api/deps.py:123`）返回的 `user_id` 和 `phone_mask`，不手填。这两个字段由 `load_user` 查出来，再放进这个 dict。

Milvus 里不存审核状态。未通过的文档没有向量，所以前台只要按 `tenant_id` 过滤。

演示用的那几篇，首次通过并生成 `chunk_id` 之后不再编辑、不再删除。标答认这些 id，一编辑就作废。

---

## 六、Phase C：问答

### C1 · 检索

`kb/search.py`：

- 文本提问：问句的独立文本向量和 BM25（问句原文）各取 20，`WeightedRanker(1.0, 1.0)` 合成 6 条。两路都带 `tenant_id == "..."`。文字问到图，靠图片块的独立图像向量，也靠描述字段上的 BM25。
- 图片提问：只对上传的图做独立图像向量，再稠密检索，过滤租户，取 6 条。检索前不再跑 `KB_CAPTION_MODEL`。先描述再融合只能和库里的融合图片块对齐，对不上纯文本块。
- 请求没带租户：不查库，直接返回固定话术。

无命中时的话术固定为「这批制度里没有这段」。不复用食疗的「确切依据」「暂未收录」。

### C2 · 生成

`kb/workflow.py` 新建一张图，三个节点：`retrieve` → `generate` → 结束。无命中则跳过生成，把固定话术写入 answer。

`generate` 只根据命中的块作答，并在回答里点出 `heading_path`。不挂食疗的 `guard` / `link` / `graph_query`。`graph/workflow.py` 不改。

### C3 · 接口

`api/kb.py`，不登录。

| 方法 | 路径 | 行为 |
|---|---|---|
| GET | `/kb/tenants` | `SELECT DISTINCT tenant_id FROM kb_document ORDER BY 1`。不登录。演示下拉要用这份清单，本期就这样。租户名会暴露给打开问答页的人 |
| POST | `/kb/ask` | 表单：`tenant_id`，以及 `question` 或一张图，二选一 |
| GET | `/kb/images/{chunk_id}` | 查询参数带 `tenant_id`。块不属于该租户、或文档不是 `approved`，返回 404 |

`/kb/ask` 的响应：`answer`、`abstained`、`hits[]`（`chunk_id`、`block_kind`、`text`、`heading_path`、`score`，图片块另给 `image_url`）。

nginx 在 `location /` 之前加 `location /kb/`，反代到 `nutri_graph_api`，`limit_req` 与 `/kg/` 相同。不写这段时，`/kb/ask` 会落到静态页，返回 `index.html`。

### C4 · trace

沿用 `llm/usage.py` 的 `span()`。`RequestIdMiddleware` 已经给每个 HTTP 请求设了 `rid`，知识库路由挂在同一个 `app` 上即可。

一次提问外面包根 span `kb_chat`，检索、图片召回、生成挂在它下面。父 span 在 `reset` 之后写入，子调用要写在 `with` 里面（`record()` 读的是当前 span）：

```python
with span("node", "kb_chat", input=question_or_image_name):
    with span("node", "kb_retrieve", input=question_or_image_name) as box:
        hits = ...
        box.output = ...
    with span("node", "kb_image", input="") as box:
        box.output = image_chunk_ids
    with span("node", "kb_generate", input="") as box:
        with purpose("kb_answer"):
            answer = ...   # 里面的 llm record() 挂在 kb_generate 下面
        box.output = answer
```

`span()` 在退出时才读 `_purpose`。`purpose("kb_answer")` 先退出，所以 **node 行的 purpose 是 `unknown`，llm 行才是 `kb_answer`**。node 行靠 `node` 字段认。这是预期，不用改 `span()`。

不新建 span 表。离线评估不进这棵树。

代码里 `TRACE_ENABLED` 默认仍是 `false`。演示机的 `.env` 改成 `true`，否则 node 行不落库，详情页看不到检索（`llm/usage.py:213`）。

---

## 七、Phase D：后台

路由挂 `require_admin`，前缀 `/admin/kb`，和 `api/admin_foods.py` 一样整组鉴权。食疗审核接口不改。

| 方法 | 路径 | 页面 |
|---|---|---|
| GET | `/admin/kb/docs` | 查询。筛选：`tenant_id`、标题子串、`doc_kind`、`uploader_phone_mask`、`status` |
| POST | `/admin/kb/docs` | 添加。multipart |
| PUT | `/admin/kb/docs/{id}` | 编辑。立刻撤向量，状态改回 `pending` |
| DELETE | `/admin/kb/docs/{id}` | 删行、删文件、删向量 |
| GET | `/admin/kb/review` | 默认 `status=pending` |
| POST | `/admin/kb/docs/{id}/approve` | 只有审核页调用 |
| POST | `/admin/kb/docs/{id}/reject` | 只有审核页调用 |
| GET | `/admin/kb/tenants` | 筛选项，来自文档表已有的 `tenant_id` |
| GET | `/admin/kb/eval/latest` | 查询页底部展示最近一次评估 |

`admin-web`：

- `Layout.vue` 增加「知识库查询」「知识库审核」。品牌文案和食疗菜单保留。
- `router.js` 增加 `kb`、`kb-review`。构建 `base` 仍是 `/console/`。
- `KbDocs.vue`：搜索、添加、编辑、删除。没有「通过」。底部一块「最近一次评估」，读 `/admin/kb/eval/latest`。不另做第三个菜单。
- `KbReview.vue`：待审核列表，通过 / 驳回。解析失败时展示 `error`，状态仍是待审核。

`doc_kind` 界面写「技术类」「制度类」，入库值是 `tech` / `policy`。块类型 `text` / `image` 不出现在这个筛选项里。

`npm run build` 后把 `admin-web/dist` 整目录覆盖到服务器 `/var/www/nutri_graph/console/`。只传 `index.html` 会留下旧的带 hash 的脚本。

---

## 八、Phase E：问答前台

新建 `kb-web/`，Vue 3 + Vite + Vue Router，依赖对齐 `admin-web/package.json`。`base` 设为 `/ask/`。

一个页面：

1. 进入时请求 `GET /kb/tenants`，下拉框展示。没有租户管理页。库是空的，下拉就是空的，先到后台添加。
2. 未选租户时，提问按钮不可用。
3. 文本框提问，或上传一张图提问。
4. 展示回答，以及命中的块。图片块用返回的 `image_url` 显示。

不登录，不出现在后台侧栏，不改 `frontend/`。

nginx：

```
location /ask/ {
    root /var/www/nutri_graph;
    try_files $uri $uri/ /ask/index.html;
}
location = /ask/index.html {
    root /var/www/nutri_graph;
    add_header Cache-Control "no-store, must-revalidate";
}
location /kb-ask/ {
    return 301 /ask/;
}
```

产物放到 `/var/www/nutri_graph/ask/`。不要覆盖 `/var/www/nutri_graph/index.html`，也不要写进 `console/`。

---

## 九、Phase F：评估

`python -m eval.kb_run`。跑完写一行 `kb_eval_run`。查询页读最新一行。终端打印不算验收。

两套输入，都是人准备的小文件，不从公开榜下载：

| 文件 | 内容 |
|---|---|
| `eval/kb_qa.jsonl` | 有据问句和参考答案。脚本调用知识库问答函数填 `response` 和 `contexts`，再跑 Ragas 的 faithfulness、context precision、answer relevancy |
| `eval/kb_pairs.jsonl` | `{"direction":"text_to_image","query":"...","expected_id":"<image chunk_id>"}` 和 `image_to_text`（`query` 是图片文件路径，`expected_id` 是文本块 id） |

Recall@K 对本次检索的 top-K 现算 R@1、R@5、R@10，并算 MRR。`expected_id` 出现在前 K 即命中。不引入 CLIP_benchmark、open_clip、NL-DIR。

Ragas 评估器继续可以用 `rag/embed.py` 的文本嵌入来比较「问题」和「生成的问题」。这个向量不拿去查知识库集合。知识库检索只走 `kb/embed_mm.py`。

不 import `AbstentionCorrectness`、`UncataloguedHandling`。

标答在演示文档首次通过之后再写 `kb_pairs.jsonl`。这批文档随后不编辑。

---

## 十、配置

`.env.example` 增加：

```
KB_COLLECTION=kb_mm_tongyi_vision_plus_1152
KB_EMBED_MODEL=tongyi-embedding-vision-plus-2026-03-06
KB_EMBED_DIM=1152
KB_LAYOUT_BACKEND=mock
KB_CAPTION_MODEL=qwen-vl-plus
KB_DATA_DIR=data/kb
KB_CHUNK_CHARS=480
KB_CHUNK_OVERLAP=48
```

`EMBEDDING_MODEL` 保持 `text-embedding-v3`。`MILVUS_CHUNK_COLLECTION` 保持 `kb_chunks`。演示机另把 `TRACE_ENABLED=true`。

---

## 十一、验收

在服务器上做。开发机不起库、不跑迁移、不跑 pytest。

```bash
cd /opt/code/nutri_graph && /opt/envs/nutri/bin/python -m scripts.migrate
cd /opt/code/nutri_graph && /opt/envs/nutri/bin/python -m pytest tests/test_kb_parse.py tests/test_kb_publish.py tests/test_kb_ask.py -q
```

手工路径：

1. 后台添加两篇，租户分别填 `tenant_a`、`tenant_b`，分类一个技术类、一个制度类。添加后状态是待审核，问答页问不到。
2. 审核通过含架构图的那篇。问「这张架构图讲了什么」能看见该图；用该图提问能命中对应文字块。
3. 同一内容的 md、pdf、docx 分成三篇，都通过。同一个问题三篇都能回到对应章节。不比较切块边界。
4. 用 `tenant_b` 问 `tenant_a` 的制度，召回为空。不选租户，接口不查库。
5. 通过后再编辑，接口一返回，这篇就问不到，状态回到待审核。驳回、删除之后同样问不到。
6. 打开后台调用追踪，根是 `kb_chat`，下面能展开检索、图片召回、生成。演示环境须已打开 `TRACE_ENABLED`。
7. 跑 `python -m eval.kb_run`，知识库查询页底部能看到 Ragas 三项和双向 Recall@K。

食疗 `/chat` 仍走原图。`kb_chunks`、`kg_entity` 的集合名和数据不变。

---

## 十二、测试落点

| 文件 | 断言 |
|---|---|
| `tests/test_kb_parse.py` | md 标题链、docx 的空 bbox、pdf 能抽出图和页码。夹具放 `tests/fixtures/kb/`，pdf 用 PyMuPDF 在测试里生成一页 |
| `tests/test_kb_publish.py` | 添加不写向量；通过失败保持 `pending`；编辑和驳回之后按 `doc_id` 删向量；缺 `tenant_id` 返回 400 |
| `tests/test_kb_ask.py` | 无租户不调用检索；错误租户结果为空；无命中话术是「这批制度里没有这段」 |

Milvus 和 DashScope 在测试里替换成假实现，避免验收时打到食疗集合。真检索放在第十一节的手工路径。
