# code_review_v10 —— plan_v10 企业知识库（图文）实现评审

> 对照 [docs/plan/plan_v10.md](../plan/plan_v10.md) 与 [docs/needs/needs_v10.md](../needs/needs_v10.md)。
> 审的是「按 plan_v10 施工完成的代码」，范围：`kb/`、`api/kb.py`、`api/admin_kb.py`、`migrations/029_kb.sql`、`eval/kb_run.py`、`kb-web/`、`admin-web` 的两个 Kb 视图、`config.py`、`app.py`、`requirements.txt`、`deploy/nginx/`、`tests/test_kb_*.py`。
> 日期：2026-11-01。未改代码。
> 【2026-11-01 修订】初版把 `kb_chunk` 缺列、问答页 404 当成两条 P0，后经 [review_v10_reply.md](review_v10_reply.md) 对着当前代码和演示机核实，两条都不成立（`page_no`/`bbox` 本就在表里；问答页代码+nginx+部署目录已统一在 `/ask/`）。本版已撤掉这两条，保留三条真偏差。

---

## 1. 结论

整体实现是**完整的、可读的、对照 plan 基本到位**的：六阶段的表/集合/解析/问答/后台/前端/评估都落了代码，`tenant_id` 注入防护、`dense` 维度核对、审核三态与向量同步、独立向量方向（`fused` 显式拒绝）、`rag/client.py` 名字不动（采纳 review_v10 §2.3）、根 span `kb_chat` + 三条子 span、无命中固定话术「这批制度里没有这段」——这些 plan 和上一轮 review 点名的关键点都正确落地。

**没有阻断性的 bug。** 剩下的都是「对照 plan 的偏差」或「留到有对照样本再补」的收尾，都不挡当前的上传和提问。三条真偏差见 §2，黄色建议见 §3。

| 级别 | 数量 | 位置 |
|---|---|---|
| 🔴 阻断 | 0 | —— |
| 🟠 对照 plan 的偏差 | 3 | 切块字数 480、Ragas 上下文缺 heading_path、Recall 缺 R@1/MRR |
| 🟡 建议 | 若干 | 见 §3 |

---

## 2. 🟠 对照 plan 的偏差

### 2.1 切块字数：plan 写 800 字，实现是 480 字

- **plan_v10 §B1**：长节用 `RecursiveCharacterTextSplitter` 切到「约 800 字、重叠 80」。
- **实现**：`config.py:98-99` `KB_CHUNK_CHARS=480`、`KB_CHUNK_OVERLAP=48`，注释理由「模型文本上限 1024 token，中文按一字一 token 估，留余量」。

这是**合理的工程收紧**（1024 token 上限下 800 字可能超限），但与 plan 数字不符。处理：代码不改回 800，把 plan/needs 里的「800 字/重叠 80」同步改成「480 字/重叠 48」，并保留「为什么」这句注释（已经写了）。

顺带说明：`kb/embed_mm.py:18` 的 `body[: config.KB_CHUNK_CHARS]` 与 `kb/milvus.py:89` 的 `text[:4000]` 截断口径不同——embed 按 480 字截断、Milvus `text` 字段按 4000 字截断。这是两层用途：稠密向量受 token 上限约束，BM25 保留更长原文。通常无害，写进 plan 即可，不用改代码。

### 2.2 `eval/kb_run.py` 的 Ragas 上下文缺 `heading_path`

`kb/workflow.py` 生成回答时，上下文是 `[heading_path] text` 拼接（`_generate`）。但 `eval/kb_run.py` 填 `retrieved_contexts` 时只取 `hit["text"]`，丢了 `heading_path`。

影响：Faithfulness / context precision 评的「依据块」和线上生成真正看到的依据**不完全一致**。评估还没跑，补齐时与 `_generate` 用同一种拼接即可。

### 2.3 Recall 缺 R@1 和 MRR

`plan_v10` §F 写明「现算 R@1、R@5、R@10，并算 MRR」。`eval/kb_run.py` 的 `_recall` 只返回 `recall_at_5` 和 `recall_at_10`。`search_question(limit=10)` 已经有名次，补 `at(1)` 和 MRR 即可。对照 plan 这是遗漏，不挡上传和提问。

---

## 3. 🟡 建议（非阻塞）

### 3.1 `kb/parse_pdf.py` 的 mock 标题判定用「中位字号 ×1.15」

`_from_pymupdf` 用整页 span 字号的中位数 ×1.15 当标题阈值，命中且 `len(text)<=40` 就置为标题并**覆盖** `heading`（`heading = [text]`，不是追加）。这会导致：同一页多个不同层级标题会被拍平成同一级，且「正文里一句短粗体」可能被误判成标题。mock 阶段可接受（plan 也说了 mock 是「结构真实」的假解析，够下游验收），但切换 `dots_mocr` 前要清楚这是简化。**可留**。

### 3.2 `kb/parse_md.py` 的图片在「行中间」会被拆成独立块

`_IMAGE.finditer(line)` 命中后先 `flush()` 再逐图 append，剩余文字 `rest` 放回 buf。符合「图抽成独立 image 块」的意图，但一行里「文字 + 图 + 文字」会被拆成「文字块 / 图块 / 文字块」三段。功能正确，只是切分颗粒度偏细。可留。

### 3.3 `kb/embed_mm.py` 对 `status_code is None` 的兜底过宽

```python
status = getattr(resp, "status_code", None)
if status not in (200, None):
    raise ...
```

`None` 兜底是为兼容不设该字段的 SDK 版本，但「返回 None 就放行」也可能掩盖真实错误。不过第 48 行已兜了空 `output`，风险可控。**低优先级**。

### 3.4 `api/kb.py` 的 `image.content_type` 未做白名单

`/kb/ask` 里 `mime = image.content_type or "image/png"` 直接透传。主要风险是「不存在的 MIME 导致嵌入失败」而非注入（DashScope 侧不接受即报错）。若要严谨，可复用 `publish._MIME` 白名单校验。**低优先级**。

### 3.5 删除文档时 `remove_files` 用 `rmtree` 删整目录

`kb/store.py` 的 `remove_files` 对 `data/kb/{doc_id}` 做 `shutil.rmtree`，源文件（`source.md` 等）和 `images/` 子目录一起去掉，删得干净。plan §A1 写「原文件放 `data/kb/{doc_id}/source`」读起来像目录，实现是 `source` + 后缀的文件，语义等价。无需改。

---

## 4. 做对了的（抽查确认，供放心）

- **独立向量方向**：`embed_mm.py` 单 content 只放 text 或只放 image，返回 `type=="fused"` 显式抛错，维度校验 1152。正确。
- **`rag/client.py` 名字没动**：`kb/milvus.py` 直接 `from rag.client import _get_client`，采纳了 review_v10 §2.3 的「不要改既有私有函数名」结论。正确。
- **`tenant_id` 注入防护**：`check_tenant` 正则 `^[A-Za-z0-9_]{1,32}$`，`doc_kind` 白名单 `tech/policy`，拼 Milvus 过滤表达式前都先过校验。正确。
- **集合维度核对**：`ensure_collection` 已存在时 `_dense_dim` 比对 `KB_EMBED_DIM`，不一致拒绝并打日志。正确。
- **审核三态与向量同步**：`add` 不写向量、`edit` 删向量回 pending、`approve` 成功才写且任一步失败删除已写入并保持 pending、`reject` 删块、`remove` 删块+文件。`publish.py` 的 try/except 清理逻辑完整。正确。
- **`kb_chunk` 表与写入列一致**：`029_kb.sql` 的 `kb_chunk` 已含 `page_no`、`bbox`，`kb/store.py:replace_chunks` 插入的列与表一一对应，审核通过链路走通。正确。
- **问答页路径**：`kb-web/vite.config.js` `base: '/ask/'`、nginx `location /ask/` 静态、产物 `/var/www/nutri_graph/ask/` 三者一致，`/kb-ask/` 301 兼容旧地址。功能可用，仅 plan 文档还写 `/kb-ask/` 待同步。正确。
- **trace**：`workflow.py` 根 `span("node","kb_chat")` 包三条子 span，`generate` 里 `purpose("kb_answer")` 包住 `invoke_model`，与 plan §C4 的预期一致。正确。
- **无租户不查库**：`api/kb.py` 直接返回固定弃答，`search.py` 又兜了一道。正确。
- **鉴权**：`admin_kb_router` 整组 `dependencies=[Depends(require_admin)]`，与 `admin_foods` 同款。正确。
- **`app.py` 挂载**：`admin_kb_router`、`kb_router` 已加进 `for _router in (...)` 元组。正确。
- **`config.py` / `.env.example`**：KB 六项变量齐了，`EMBEDDING_MODEL` 仍 `text-embedding-v3`、`MILVUS_CHUNK_COLLECTION` 仍 `kb_chunks`、`TRACE_ENABLED` 默认 false。正确。
- **`requirements.txt`**：`pymupdf`、`python-docx` 已加，`dashscope` 已有，未加 CLIP/vLLM。正确。
- **测试**：三个测试文件覆盖「标题链 / docx 空 bbox / pdf 抽图」「添加不写向量 / 通过失败保持 pending / 编辑驳回删向量 / 缺租户 400」「无租户不检索 / 错误租户空 / 无命中话术」，Milvus/DashScope 用 monkeypatch 假实现，测试库 10 条通过（含审核通过）。符合 plan §十二 的断言落点。

---

## 5. 接下来要做的（收尾，不阻断）

1. **改 plan 文档**（代码保持现状）：
   - `plan_v10` 的问答页路径从 `/kb-ask/` 改成 `/ask/`，产物目录改成 `/var/www/nutri_graph/ask/`。
   - `plan_v10` 的切块从「约 800 字、重叠 80」改成「480 字、重叠 48」，写明原因是模型文本上限 1024 token。
2. **评估补齐**（有了人工对照样本之后）：
   - `eval/kb_run.py` 的 Ragas `retrieved_contexts` 带上 `heading_path`，与 `_generate` 同格式。
   - `_recall` 补 `recall_at_1` 和 MRR。
