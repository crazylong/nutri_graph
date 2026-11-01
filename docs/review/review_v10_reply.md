# code_review_v10 回复

> 对照 [docs/review/code_review_v10.md](code_review_v10.md)、[docs/plan/plan_v10.md](../plan/plan_v10.md)，以及当前代码与演示机页面。
> 核对过：`migrations/029_kb.sql`、`kb/store.py`、`kb/workflow.py`、`eval/kb_run.py`、`kb-web/vite.config.js`、`deploy/nginx/nutri-graph-locations.conf`。
> 演示机已打开 [http://8.134.253.167/ask/](http://8.134.253.167/ask/)。测试库跑过 `tests/test_kb_parse.py`、`tests/test_kb_publish.py`、`tests/test_kb_ask.py`，10 条通过，其中包含审核通过。
> 未改代码。日期：2026-11-01。

---

## 1. 结论

`code_review_v10` 里「做对了什么」和黄色建议大体成立。两条被标成必须改、并写进开工清单 P0 的结论，对着现在的代码和线上页面不成立：

1. `kb_chunk` 并没有缺 `page_no` / `bbox`，审核通过不会因为缺列失败。
2. 问答页不是「`/ask/` 和 `/kb-ask/` 各改一半」的 404。代码、nginx、部署目录已经统一在 `/ask/`。和计划不一致的只剩计划文档仍写 `/kb-ask/`。

切块字数、Ragas 上下文、R@1 / MRR 这三处是真偏差，都不挡住现在的上传和提问。

---

## 2. 两条 P0 不成立

### 2.1 `page_no` / `bbox` 已经在表里

评审 §3.3 说 `migrations/029_kb.sql:26-38` 的 `kb_chunk` 没有 `page_no` 和 `bbox`，而 `kb/store.py` 的插入语句写了这两列，所以审核通过必失败。

当前迁移就是这两列：

```26:38:migrations/029_kb.sql
CREATE TABLE IF NOT EXISTS kb_chunk (
    id           TEXT PRIMARY KEY,
    doc_id       UUID NOT NULL REFERENCES kb_document(id) ON DELETE CASCADE,
    tenant_id    TEXT NOT NULL,
    block_kind   TEXT NOT NULL,
    text         TEXT NOT NULL,
    page_no      INT,
    bbox         JSONB,
    heading_path TEXT NOT NULL DEFAULT '',
    image_path   TEXT,
    image_md5    TEXT,
    CONSTRAINT kb_chunk_kind CHECK (block_kind IN ('text', 'image'))
);
```

`kb/store.py` 的 `replace_chunks` 插入的列与这张表一致。测试库执行过这条迁移，`test_kb_publish` 里的通过、编辑、驳回都过了。这条从开工清单里拿掉，不用再补迁移。

### 2.2 问答页已经统一在 `/ask/`

评审 §2.1 把 `base: '/ask/'`、nginx 的 `/ask/` 静态、`/kb-ask/` 的 301 看成半改状态，并推断：若产物仍按计划放到 `/var/www/nutri_graph/kb-ask/`，则 `/ask/` 没有 `index.html`，页面 404。

这是按计划文档推断的部署，不是现在的部署。三处已经对齐：

- `kb-web/vite.config.js` 的 `base` 是 `/ask/`，构建产物引用 `/ask/assets/...`。
- nginx 用 `location /ask/` 指向 `/var/www/nutri_graph`，`index.html` 单独 `no-store`。产物在 `/var/www/nutri_graph/ask/`。
- `/kb-ask/` 301 到 `/ask/`。浏览器打开旧地址会落到新地址。

页面已打开并点过「提问」：没选租户时停在「请先选择租户」。评审给出的修法 A（改回 `/kb-ask/`）与后来定下的地址相反，不采用。修法 B 里「代码和 nginx 改到 `/ask/`」已经做完。剩下的是把 `plan_v10` 里仍写着 `/kb-ask/` 的句子改成 `/ask/`，避免下次按计划把产物传到旧目录。

---

## 3. 成立的偏差

### 3.1 切块字数：计划 800，代码 480

`plan_v10` 的解析一节仍写「约 800 字、重叠 80」。实现是 `KB_CHUNK_CHARS=480`、`KB_CHUNK_OVERLAP=48`。480 是按模型 1024 token 上限收的：中文按一字一 token 估，800 字可能超限。这是有意收紧，代码不改回 800。把计划里的数字改成 480 / 48，并写上这个原因。

评审还指出嵌入按 480 字截断、Milvus 的 `text` 字段按 4000 字截断。这是两层用途：稠密向量受 token 上限约束，BM25 保留更长原文。通常无害，写进计划即可，不用改代码。

### 3.2 Ragas 上下文少了章节路径

线上生成用的是 `[heading_path] text`（`kb/workflow.py` 的 `_generate`）。`eval/kb_run.py` 填 `retrieved_contexts` 时只取了 `hit["text"]`。Faithfulness 和 context precision 看到的依据，比回答模型看到的少了章节路径。评估还没跑，补齐时与 `_generate` 用同一种拼接。

### 3.3 Recall 缺 R@1 和 MRR

`plan_v10` 写明现算 R@1、R@5、R@10，并算 MRR。`eval/kb_run.py` 的 `_recall` 只返回 `recall_at_5` 和 `recall_at_10`。`search_question(limit=10)` 已经有名次，补 `at(1)` 和 MRR 即可。对照计划这是遗漏，不挡住上传和提问。

### 3.4 删除文件、mock 标题、其余建议

- 删除文档时 `remove_files` 对 `data/kb/{doc_id}` 做 `rmtree`，源文件和 `images/` 一起去掉。源文件名是 `source` 加后缀（如 `source.md`），计划里的 `source` 读起来像目录。删得干净，不用改。
- mock 用中位字号标标题、Markdown 行内图片拆块、`status_code is None` 放行、提问图片的 MIME 未做白名单：都是评审里的低优先级观察，判断合理，现在不动。
- 独立向量、不改 `_get_client`、租户正则、审核三态与向量同步、根 span `kb_chat`、无租户不查库、管理员鉴权、食疗嵌入配置不动：这些抽查与代码一致。

---

## 4. 接下来改文档，评估补齐留到有对照样本之后

1. `plan_v10` 的问答页路径从 `/kb-ask/` 改成 `/ask/`，产物目录改成 `/var/www/nutri_graph/ask/`。代码和 nginx 保持现状。
2. `plan_v10` 的切块从「约 800 字、重叠 80」改成「480 字、重叠 48」，写明原因是模型文本上限 1024 token。
3. 有了人工对照之后再改 `eval/kb_run.py`：Ragas 上下文带上章节路径，Recall 补上 R@1 和 MRR。
