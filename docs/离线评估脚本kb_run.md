# 离线评估脚本 kb_run.py

`eval/kb_run.py` 是「企业知识库」的**离线评估脚本**：用事先人工准备好的对照题，离线检验知识库问答质量，跑一次把结果写进 `kb_eval_run` 表，管理后台「知识库查询」页的「最近一次离线评估」卡片读取的就是这张表。

## 它评什么

两类指标：

| 指标 | 数据来源 | 说明 |
|---|---|---|
| **Recall@K**（人工对照） | `eval/kb_pairs.jsonl` | 检验**检索**能不能把「标准答案块」捞出来，输出 R@1 / R@5 / R@10 / MRR。**不调 LLM，零费用**。 |
| **Ragas 三项**（有据回答才评） | `eval/kb_qa.jsonl` | 检验**生成答案**的质量：忠实度 faithfulness、答案相关性 answer_relevancy、上下文精确度 llm_context_precision。**会调 LLM，产生费用**。 |

> 说明：`eval/metrics.py` 里还定义了 `AbstentionCorrectness`（该弃答时是否弃答）、`UncataloguedHandling`（未收录食材是否诚实登记不编造）两个纯规则指标，但 `kb_run.py` 里 `_ragas()` 只取 `build_metrics(...)[:3]`，**前三个指标**，后两个当前未启用。

## 怎么跑

在**服务器上**、**文档已经审核通过之后**手动执行：

```bash
python -m eval.kb_run
```

返回示例：

```json
{
  "ragas": {"n": 3, "faithfulness": 0.92, "answer_relevancy": 0.81, "llm_context_precision_without_reference": 0.77},
  "recall": {"n": 10, "recall_at_1": 0.70, "recall_at_5": 0.90, "recall_at_10": 1.0, "mrr": 0.83}
}
```

## 什么时候跑

- **在管理后台「知识库查询」页点「手动触发评估」按钮**，后台线程跑，跑完结果自动写库、页面刷新可见。
- 也可以在服务器上手动执行 `python -m eval.kb_run`（效果相同）。

> 历史说明：曾配置过 systemd timer 每小时自动跑，因「无新增数据也会持续调模型产生费用」已停用，改为纯手动触发。

### 关键前提（务必注意）

1. **`expected_id` 是审核时生成的 `chunk_id`**。文档**通过审核之后不要再编辑或重新通过**这些文档，否则块 id 会变，`expected_id` 就失效了，脚本会报「找不到块」。
2. 租户不需要手动填在 pairs 里——脚本会从 `expected_id` 对应的 `kb_chunk` 反查租户。
3. Ragas 那步要调模型，需要 `.env` 配好 `DASHSCOPE_API_KEY`；没配则返回 `{"skipped": "未配置 DASHSCOPE_API_KEY"}`。

---

## 两个样本文件

评估完全由这两个固定文件驱动。脚本**不扫描知识库有没有新增文档**，也不做增量——每次跑的内容就是这两个文件里写死的题目。要评测新文档，需要往文件里加对应题目。

### 1. `eval/kb_pairs.jsonl` —— 检索召回对照

每行一道题，格式：

```json
{"question": "公司的出差报销标准是多少？", "expected_id": "a1b2c3d4e5f6..."}
{"question": "新员工入职需要提交哪些材料？", "expected_id": "f6e5d4c3b2a1..."}
```

| 字段 | 必填 | 说明 |
|---|---|---|
| `question` | 是 | 用户会问的问题 |
| `expected_id` | 是 | 期望被检索命中的 **chunk_id**（审核通过时生成） |

**作用**：脚本对每道题跑一次 `search_question`（向量检索，取 top10），看 `expected_id` 有没有出现在结果里、排第几，据此算 R@1/R@5/R@10 和 MRR。**纯检索，不调 LLM，不花钱。**

### 2. `eval/kb_qa.jsonl` —— 生成质量打分

每行一道题，格式：

```json
{"question": "公司年假怎么休？", "reference": "员工入职满一年可享 5 天带薪年假。", "tenant_id": "default_renter"}
{"question": "医保报销比例是多少？", "reference": "ABSTAIN", "tenant_id": "default_renter"}
```

| 字段 | 必填 | 说明 |
|---|---|---|
| `question` | 是 | 用户会问的问题 |
| `reference` | 否 | 参考答案。普通题填标准答案；填 `"ABSTAIN"` 表示「此题应拒答」（文档里没有依据）。当前 `kb_run` 只评前三项指标，`reference` 主要起留档/备用作用，前三个指标基本不依赖它 |
| `tenant_id` | 是* | 归属租户。若不填，可以带 `expected_id` 让脚本从块上反查租户 |

> \* 二选一：要么每行写 `tenant_id`，要么写 `expected_id`（脚本从 `kb_chunk` 反查租户）。两者都没有会直接报错退出。

**作用**：脚本对每道题跑一次 `run_ask`（真实问答链路），只对有据回答的题用 ragas 打三项分，取平均。**会调 LLM，产生费用。**

### 示例文件里 `expected_id` 从哪拿

审核通过后，块 id 存在 `kb_chunk` 表里，可以这样查（拿某篇文档的所有块 id）：

```sql
SELECT id, heading_path, text
FROM kb_chunk
WHERE doc_id = '<文档id>'
ORDER BY page_no, id;
```

挑一条你想作为「标准答案」的块，把它的 `id` 填进 `kb_pairs.jsonl` 的 `expected_id` 即可。

---

## 费用问题（FAQ）

**Q：它是增量拉取数据吗？**
A：不是。它只读 `kb_pairs.jsonl` / `kb_qa.jsonl` 两个固定文件，**不扫描知识库有没有新增文档**。新增了 100 篇文档但不改这两个文件，跑出来的结果和上次一模一样；反之没加任何文档，文件里有题就照跑。

**Q：每小时跑一次，没有新增数据会花钱吗？**
A：花不花钱**不取决于有没有新增数据**，而取决于 `kb_qa.jsonl` 里有没有题：

| 情况 | 是否调模型 | 费用 |
|---|---|---|
| 两个文件都空 / 都不存在 | 否（直接打印「没有评估样本」返回） | 零 |
| 只有 `kb_pairs.jsonl` 有题 | 否（只走向量检索） | 零 |
| `kb_qa.jsonl` 有题 | 是（`run_ask` + ragas 打分都调 LLM） | **产生费用，每小时一次** |

**当前状态**：服务器上这两个文件都不存在，所以现在每小时的任务是「空转」——每小时打一行「没有评估样本」日志就结束，零费用。等你往 `kb_qa.jsonl` 里加了题，才会开始每小时真实调模型花钱。

---

## 手动触发的实现

评估已改为**纯手动触发**，通过后台接口暴露给管理页面：

| 接口 | 方法 | 说明 |
|---|---|---|
| `POST /admin/kb/eval/run` | POST | 触发一次评估。异步执行，立即返回 `{"started": true}`；正在跑时返回 409 `eval_running` |
| `GET /admin/kb/eval/status` | GET | 运行状态：`{"running": bool, "error": str\|null}` |
| `GET /admin/kb/eval/latest` | GET | 最近一次结果（读 `kb_eval_run` 表） |

- 后台线程跑 `eval.kb_run.run_eval()`，结果写 `kb_eval_run` 表；异常记到 status 的 `error`。
- 前端「知识库查询」页的「手动触发评估」按钮会调 `/eval/run`，然后轮询 `/eval/status`，跑完自动刷新 latest。
- 原 systemd 定时任务（`nutri-graph-eval.timer`，每小时）**已停用并 disable**，文件仍保留在 `deploy/systemd/` 下但不启用。
