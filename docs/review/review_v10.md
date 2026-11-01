# plan_v10 评审（对照 needs_v10）· 第二轮

> 对照 [docs/needs/needs_v10.md](../needs/needs_v10.md)（第三节 3.3 是施工口径）与 [docs/plan/plan_v10.md](../plan/plan_v10.md)。
> 核对过现有代码：`llm/usage.py`（`span` / `record` / `purpose` / `_insert`）、`api/deps.py`（`require_admin` / `load_user`）、`config.py`（集合名、`LLM_MODEL_POOL` 注释）、`app.py`（路由挂载）、`rag/client.py`（`_get_client` / `_ensure_collection`）。
> 未改代码。日期：2026-11-01。
> 这是第二轮：plan 已吸收第一轮 review 的大部分结论（根 span、行号、图片阈值、独立向量方向）。
> §一 是结论，§二 是开工前要定/改的点，§三 是可以照做的。
> 【2026-11-01 追加】§2.1 的「融合 vs 独立向量」已由用户拍板：**选独立向量**。needs_v10 的 3.3（原 226 行）、2.8 第 2 条、KB-02、2.1/2.2/2.6 表格已全部同步为独立向量口径，与 plan 对齐。此条状态从「必须定」转为「已定」。

---

## 1. 结论

第二轮 plan 整体质量比第一轮好：根 span `kb_chat` 补上了（C4）、`require_admin` 行号改对（`api/deps.py:123`）、图片阈值从「8MB 整篇不通过」改成「5MB 建议 / 10MB 硬上限，且超限不悄悄丢图」，这些第一轮点名的问题都处理了。

第二轮的唯一点名问题——「plan 改独立向量、needs 仍写融合向量」的方向性冲突——**已经定案并同步**：采纳 plan 的**独立向量**方向，needs_v10 相关小节已同步修改，两份文档口径一致。详见 §2.1。

其余都可照写。§2.2~2.4 是三处小口径要顺手改掉。

---

## 2. 开工前定 / 改

### 2.1 嵌入走「独立向量」（已定案）

**结论**：**选独立向量**。plan A3/C1 的方向正确，needs_v10 已同步（3.3、2.8 第 2 条、KB-02、2.1/2.2/2.6 表格全部改为独立向量口径）。下面是定案过程记录，供追溯。

**needs 的原口径**：

- 第三节 3.3（`needs_v10.md:226`）：「稠密向量打在『图 + 描述』的**融合嵌入**上。」
- 2.8 第 2 条（`needs_v10.md:149`）：「注意必须用**带日期后缀**的快照版本——不带后缀的只支持『独立向量』，不支持『图+描述融合向量』。」

**plan 的口径（新写的）**：

- A3（`plan_v10.md:129-134`）：「检索只用**独立向量**……融合向量是另一种返回类型（`fused`），文档没有保证它能和纯文本向量比。一次 content 里同时放 text 和 image 会得到融合向量。**本计划不这么调用**。」
- C1（`plan_v10.md:191`）：「图片提问：只对上传的图做独立图像向量……检索前不再跑 `KB_CAPTION_MODEL`。先描述再融合只能和库里的融合图片块对齐，对不上纯文本块。」

**为什么这是个真冲突，不是措辞差异**：两条路指向**完全不同的入库向量和提问向量**，直接影响「文本问到图 / 图问到文字」能不能成立，也直接影响验收第 2 条怎么写。

**两条路各自的逻辑，以及我核对后的判断**：

- **独立向量路（plan 现在选的）**：百炼对 `tongyi-embedding-vision-plus` 的「独立向量」定义是「以文搜图、以图搜文」——文本块用文本塔、图片块用图像塔，两塔各自出向量，靠模型内部的图文对齐让文本 query 和图片 document 落在可比的同一空间。**这条路是模型官方支持的用法**，比第一轮担心的「自造图+描述融合再和纯文本比」更稳，因为它压根不造融合向量，就不存在「融合向量 vs 纯文本向量不可比」的问题。plan 的 A3 这段技术判断是对的。
- **融合向量路（needs 现在写的）**：图 + 描述放同一个 content 出一个 `fused` 向量，图片块只有一个向量。这条路的问题是：**图片提问时用户只给纯图、没有描述**，融合向量要和「纯图 query」比，就回到了第一轮 review 已经指出的「纯图 vs 图+描述」可比性风险。

**采纳理由**：

1. 独立向量是百炼官方对「以文搜图/以图搜文」的**明确语义**，plan 引用准确；
2. 它彻底绕开了第一轮纠结的「纯图 vs 图+描述融合」可比性问题——图片提问就是纯图 → 图像塔向量，图片入库也是纯图 → 图像塔向量，**完全对称**；
3. 图片块仍生成上下文描述，但描述只写进 `text` 字段供 BM25 和回答用，**不参与稠密向量**（plan A3 第 134 行已这么写）——这样「文本搜到图」靠两条腿：图像塔对齐（语义）+ BM25 描述（关键词），比单靠融合向量更扎实。

**唯一要补的一个验证点（仍未落到 plan，建议动工时做）**：独立向量路依赖「百炼对 中文文档图（架构图/流程图）的文本↔图对齐质量」。这个不是写文档能定死的，**建议在 Phase A 动工时先拿 1~2 张真实架构图 + 1 句中文问句，实测一次 `MultiModalEmbedding` 的独立向量召回**，命中再全量铺开。plan 目前没写这个 smoke test，建议补进 B 或 C 的入口。

**定案结果**：**已选独立向量**。needs_v10 的 3.3、2.8 第 2 条、KB-02、2.1/2.2/2.6 表格已全部改为独立向量口径，与 plan 对齐，不再有冲突。

### 2.2 needs 第三节 3.3 的「融合」已改（连带 2.1，已处理）

原 `needs_v10.md:226`「稠密向量打在『图 + 描述』的融合嵌入上」已改为「稠密向量打在图片的图像塔独立向量上、不调用融合向量」。2.8 第 2 条（原 149 行）整段已重写，选模型理由从「必须用融合向量版快照」改为「本主题只走独立向量、不调用融合向量」。2.1 表 KB-02、2.2 表、2.6 借鉴表也同步改了措辞。**此条已闭环，无需再动。**

注：改了独立向量后，原来 2.8 里「不带后缀的版本只支持独立向量、不支持融合」那句因果已不再适用，已在重写时移除。

### 2.3 `rag/client.py` 改名 `get_client` 的副作用要确认

plan 三、`rag/client.py:27` 写「`_get_client` 改名为 `get_client` 并给知识库用」。核对代码：`_get_client` 是模块内私有函数，被 `_ensure_collection`、`insert_entity`、`insert_chunk` 等多个函数调用（`rag/client.py:27-57` 及之后）。改名 `get_client` 会把一个模块私有的懒加载单例暴露成公共名，且要同步改所有内部调用点。这不是错，但：

- 改名**没必要**——`kb/milvus.py` 计划里是用「显式 schema、不用 `_ensure_collection`」，它需要的是 `MilvusClient` 连接，可以自己 `from rag.client import _get_client`（同包可访问下划线名），或直接在 `kb/milvus.py` 里独立建一个 client（计划 A2 说「新集合用显式 schema」本来就该独立建）。**建议不要动 `rag/client.py` 的既有函数名**，减少对食疗线的改动面——改一个被多处引用的私有函数名，属于没必要的扩散。
- 若坚持复用同一个 `MilvusClient` 单例（连接复用有好处），那就 `kb/milvus.py` 里 `from rag.client import _get_client` 即可，**名字不用改**。

### 2.4 `kb_chunk.id` 用 TEXT、`kb_document.id` 用 UUID，`chunk_id` 生成时机

A1 里 `kb_chunk.id TEXT PRIMARY KEY`（审核通过时生成），而 `kb_document.id` 是 UUID。这个「通过时生成块 id」和 B2「标答认这个 id、一编辑就作废」是一致的，没问题。但要注意一个衔接：**`eval/kb_pairs.jsonl` 的 `expected_id` 是 `image chunk_id`（文本）**，而块 id 是「审核通过时」才生成的随机值。这意味着：

- 标答必须在「演示文档首次通过之后」再写（plan F 已写，正确）；
- 但**通过动作如果重跑（比如解析失败重试通过），块 id 会不会变？** B2 写「通过：成功才 approved，失败删已写入向量保持 pending」，重试通过会重新生成 chunk_id。所以标答和「首次通过」绑定，一旦重跑通过，旧标答的 `expected_id` 就失效。plan 已用「这批文档随后不编辑、不删除」兜住，但**建议在 F 里再补一句「标答冻结依赖块 id 稳定；若某篇通过后重跑过，`kb_pairs.jsonl` 要重写」**，避免评估跑出「expected_id 不在库里」的假阴性。

---

## 3. 可以照做的

以下逐条核对过代码，准确，可直接照写：

- **根 span `kb_chat`**：C4 补了 `span("node", "kb_chat", input=...)` 包住三条子 span，解决了第一轮「三条平级裸根」的问题。`span()` 的 `parent = _current_span.get()` 在进入时取父、`finally` 里 `_current_span.reset(token)` 再写行（`llm/usage.py:288-306`），示例写法正确。
- **node 行 purpose 是 unknown**：C4 已明确写出「node 行靠 `node` 字段认，llm 行才是 `kb_answer`，属预期不用改 `span()`」。这句是对的——`span()` 的 `finally` 里 `pur = _purpose.get()`（`usage.py:302`），此时内层 `purpose` 已 reset。第一轮 §2.2 的顾虑已解决。
- **`require_admin` 行号**：B2 改成 `api/deps.py:123`，正确。`require_admin`（`deps.py:123`）返回 `get_current_user` 的 `user` dict，`load_user`（`deps.py:51-63`）里含 `user_id`、`phone_mask`、`role`、`status`。plan 写「这两个字段由 `load_user` 查出来再放进 dict」准确。
- **`TRACE_ENABLED` 默认 false**：C4 引用 `llm/usage.py:213`——`_insert` 里 `if kind != "llm" and not config.TRACE_ENABLED: return`，node/tool 行在关闭时不落库，llm 行落库但入参出参置空。引用准确。
- **`app.py:119-122` 路由挂载**：`for _router in (...)` 循环在 `app.py:119-123`，plan 写 119-122 基本对（循环体到 123 行）。新增 `kb_router` / `admin_kb_router` 加进这个元组即可。
- **`config.py:36-37` 集合名**：`MILVUS_ENTITY_COLLECTION`（36 行）、`MILVUS_CHUNK_COLLECTION`（37 行）确实在这，新集合名和 `KB_COLLECTION` 放附近合理，且不改 `EMBEDDING_MODEL` 默认值（`text-embedding-v3`）正确。
- **两套嵌入隔离**：`kb/embed_mm.py` 不调 `rag/embed.py` 的 `get_embeddings()`（那条是 `text-embedding-v3` 且带食疗缓存），复用 `DASHSCOPE_API_KEY`。正确。
- **描述模型不放 `LLM_MODEL_POOL`**：`KB_CAPTION_MODEL` 默认 `qwen-vl-plus` 单独配置，不放进池——`config.py:105` 注释确实写明视觉/视频模型协议不兼容。引用准确。
- **混合检索**：`pymilvus>=3.0.2` 的 `MilvusClient` + `AnnSearchRequest` + `WeightedRanker` + `hybrid_search`，BM25 的 `sparse` 用 `SPARSE_INVERTED_INDEX` 由服务端从 `text` 生成，不搬参考项目 `db_retriever.py`（参考项目锁 `pymilvus==2.6.0`）。正确。
- **集合名**：`kb_mm_tongyi_vision_plus_1152` 只用字母数字下划线，规避连字符硬伤；集合已存在时核对 `dense` 维度、不一致拒绝写入并打日志。正确。
- **审核三态与向量同步**：`添加不写 / 编辑删块回 pending / 通过才写块且失败保持 pending 并删已写入 / 驳回不留块 / 删除删块+目录`。Milvus 不存审核状态、未通过无向量 → 前台只按 `tenant_id` 过滤，这个「状态由向量有无隐式表达」的简化是对的。
- **`tenant_id` 过滤表达式**：只允许 `^[A-Za-z0-9_]{1,32}$`，避免拼进 Milvus 过滤表达式造成注入。正确。
- **无召回话术**：「这批制度里没有这段」单独写，不复用食疗「确切依据」「暂未收录」，不 import `AbstentionCorrectness` / `UncataloguedHandling`。正确。
- **评估**：`eval/kb_run.py` 跑完写 `kb_eval_run`，查询页读最新一行，终端打印不算验收；Ragas 复用 `rag/embed.py` 文本嵌入比较「问题 vs 生成的问题」、不拿去查知识库集合；Recall@K 对 `kb_pairs.jsonl` 现算 R@1/R@5/R@10 + MRR，不引 CLIP_benchmark / open_clip / NL-DIR 依赖。正确。
- **前端**：`kb-web/` 独立 Vue 应用、`base=/kb-ask/`，不进 `/console/` 登录闸门、不覆盖 `frontend/`；`admin-web` 加两个菜单、构建后整目录覆盖 `console/`（「只传 index.html 会留旧 hash 脚本」提醒正确）。
- **nginx**：`location /kb/` 写在 `location /` 之前，否则 `/kb/ask` 落到静态页返回 index.html；`/kb-ask/index.html` 单独 `no-store, must-revalidate`。正确。
- **测试与验收**：服务器上跑迁移 + pytest，Milvus 与 DashScope 测试里替换成假实现，真检索走手工路径。符合「本机不跑测试」铁律。
