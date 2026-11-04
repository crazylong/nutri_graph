# 测试 SOP —— RAG 评估指标实操流程

> 目的：给 RAG / 知识库系统跑一套**可复现、可量化、能归因**的评估，拿到「召回率 / 忠诚率 / 相关性 / 拒答正确率」等指标基线，并形成「调优 → 复测 → 对比」的闭环。
>
> 适用：本项目 `eval/` 目录下的评估链路（`run.py`、`kb_run.py`、`metrics.py`），以及企业知识库（`kb/`）的离线评估。
>
> 核心原则：**先定基线 → 定评估集 → 定指标口径 → A/B 对比 → 归因**。数字只有能说清「怎么测、怎么算、怎么归因」，才是有效指标。

---

## 0. 指标全景速查（先分清"评什么"）

RAG 的指标要分**两个层次**看，别混在一起：

| 层次 | 回答的问题 | 核心指标 | 是否依赖 LLM |
|---|---|---|---|
| **检索层** | 该找的文档/块，找没找到、排没排前？ | Recall@K、MRR、Precision@K | 否（规则） |
| **生成层** | 找对了，答得对不对、有没有瞎编？ | Faithfulness（忠诚率）、AnswerRelevancy、ContextPrecision | 是（LLM 评） |
| **拒答层** | 该拒答时拒没拒？未收录时编没编？ | AbstentionCorrectness、UncataloguedHandling | 否（规则） |

> 一句话记忆：
> - **召回率（Recall@K）** = 检索层，top-K 里命中了多少"正确答案对应的块"。
> - **忠诚率（Faithfulness）** = 生成层，回答里的每句 claim 能不能在检索到的上下文里找到依据（**反幻觉第一指标**）。

---

## 1. 先定基线（Baseline First）

任何调优之前，必须先在**固定评估集**上跑出基线数字，否则后面说"提升 50%"没有对照。

```bash
# 知识库离线评估（服务器上、文档已过审核后）
python -m eval.kb_run
```

产出：`ragas`（生成层三项 + 拒答正确率）和 `recall`（检索层 R@1/R@5/R@10 + MRR）两组数字，并写入 `kb_eval_run` 表留痕。

**基线要记录的东西：**
- 检索配置：当前是纯向量？还是混合（稠密 + BM25）？top-k 取多少？
- 分块策略：块大小、是否带图、近邻块是否开启。
- 嵌入模型：哪个 embedding。
- 生成模型 + 是否用了图谱约束。

> 基线不记录配置，后面的对比就无从归因。这是最常见的翻车点。

---

## 2. 定评估集（两条线，分开建）

项目里已有两类评估集文件，对应两种指标：

### 2.1 检索层评估集 —— `kb_pairs.jsonl`

一行一个「问题 → 正确答案块」对照：

```json
{"question": "员工住宿标准是多少？", "expected_id": "<审核时生成的 chunk_id>"}
```

- `expected_id` 是**审核通过时生成的 chunk_id**，从对应 `kb_chunk` 读。
- ⚠️ **通过审核后不要再编辑或重新通过这些文档**，否则 chunk_id 会变，对照全失效。
- 建集原则：**从线上真实用户查询脱敏抽样 + 业务同学补充典型问法**，一两百条真实标注即可，覆盖核心场景优先于数量。

### 2.2 生成层评估集 —— `kb_qa.jsonl`

一行一个「问题 → 期望行为」：

```json
{"question": "……", "tenant_id": "……", "reference": "……"}
```

`reference` 决定这题归到哪类、用哪套指标评：

| reference | 含义 | 评估指标 |
|---|---|---|
| 空 / 其它 | 有据回答（grounded） | faithfulness / answer_relevancy / context_precision（LLM 三项） |
| `"ABSTAIN"` | 应弃答（图谱无依据） | abstention_correctness（规则） |
| `"UNCATALOGUED"` | 未收录食材 | uncatalogued_handling（规则） |

> **分层评估是关键**：弃答题、未收录题的正确答案是"拒答/登记"，没有可验证的 claim，混进有据题一起算 faithfulness 会把分数拖到一个误导性的低分。所以必须分类评。

---

## 3. 定指标口径（每个指标到底怎么算）

### 3.1 检索层：Recall@K + MRR（规则，可复现）

代码：`kb_run.py` 里的 `score_ranks()`。

| 指标 | 算法 | 说明 |
|---|---|---|
| **Recall@1 / @5 / @10** | 期望块在 top-K 命中名次 ≤ K 的样本占比 | K 越小越严格 |
| **MRR** | 每个样本「期望块实际名次的倒数」的平均 | 反映"排多前"，命中但排第 10 名和排第 1 名差距很大 |

```python
def at(k): return sum(1 for rank in ranks if rank is not None and rank <= k) / total
mrr = sum((1.0 / rank) if rank else 0.0 for rank in ranks) / total
```

**口径要点：**
- 没命中（rank=None）→ Recall 记为 0，MRR 记为 0，**不计入分母的"命中"里**。
- 重嵌后旧 chunk_id 失效时，同一张图的另一行（image_path 相同）也判命中（`_image_path` 兜底）。

### 3.2 生成层：忠诚率 / 相关性（LLM 评）

代码：`metrics.py` 里的 `build_metrics()`。

| 指标 | 中文 | 评什么 | 防什么 |
|---|---|---|---|
| **Faithfulness** | 忠诚率 / 忠实度 | 回答的每条 claim 能否在 retrieved_contexts 里找到依据 | **幻觉（瞎编）** |
| **ResponseRelevancy** | 答案相关性 | 回答是否切题、有没有答非所问 | 跑题 |
| **LLMContextPrecisionWithoutReference** | 上下文精度 | 检索到的上下文里，真正有用的占比 | 召回"掺水" |

> **忠诚率是反幻觉的第一指标**。它衡量的是：模型有没有把检索到的上下文之外的东西（自己的知识）掺进答案。忠诚率低 = 幻觉风险高。

### 3.3 拒答层：规则判定（不调 LLM，可复现）

| 指标 | 评什么 | 判定方式 |
|---|---|---|
| `abstention_correctness` | 该弃答时是否真弃答（而非给推测性建议） | 关键词规则（"确切依据""无法回答"等） |
| `uncatalogued_handling` | 未收录食材是否「诚实登记 + 不编造数值 + 不做疗效断言」 | 三项子规则各 0/1 取平均 |

> 弃答正确率用**确定性规则**而非 LLM：可复现、零成本。缺点是规则对措辞敏感，改话术要同步维护规则（见 §7 踩坑）。

---

## 4. 跑评估（实操命令）

### 4.1 知识库离线评估（推荐入口）

```bash
# 服务器上，文档审核通过后
python -m eval.kb_run
```

产出两组：`recall`（R@1/R@5/R@10/MRR）+ `ragas`（faithfulness/relevancy/context_precision + abstention_correctness），自动写 `kb_eval_run` 表。

### 4.2 食疗链路评估（`eval/run.py`）

```bash
# 用真实链路先跑一遍再评（需要 Neo4j / Milvus / LLM 全就绪）
python -m eval.run --dataset eval/dataset.jsonl --run-chain

# 只评已有 response（不跑链路，用于快速复测）
python -m eval.run --dataset eval/dataset.jsonl
```

> ⚠️ 不传 `--run-chain` 时，若 `dataset.jsonl` 的 response 是空串，评的其实是"空回答"，分数无意义。

---

## 5. 复测与 A/B 对比（调优 → 复测 → 归因）

这是"提升 50%"能站得住脚的关键一步：

1. **只改一个变量**（比如只把纯向量改成混合检索），其余配置冻结。
2. **同一份评估集、同一个 K 值**下重跑 `kb_run.py`。
3. 对比基线：**相对提升 = (新值 - 基线) / 基线**，同时报**绝对提升**（新值 - 基线）。

```
例：基线 Recall@10 = 0.40 → 优化后 0.60
    相对提升 = 50%，绝对提升 = +20 个百分点
```

> 面试被问"50% 是相对还是绝对"时，主动说清是**相对提升**，并补一句绝对百分点，最显专业。

4. **归因**：提升来自哪，逐条对应到改动：
   - 多路召回 → 补上向量检索对精确词/专有名词的短板
   - 重排序 → 把真相关的提到前面
   - 分块/近邻块策略 → 让召回粒度更准

5. **防过拟合**：留一个**不参与调参的 holdout 集**做验证；上线后再看线上反馈（无效回答率、用户点击满意度）是否同步改善。

---

## 6. 指标解读：一个数字怎么读

| 现象 | 含义 | 下一步 |
|---|---|---|
| Recall 高、Faithfulness 低 | 找得对但爱瞎编 | 加引用约束 / 图谱约束 / 降温度 |
| Recall 低、Faithfulness 高 | 找不全，但答得保守 | 优化检索：混合检索、重排序、近邻块 |
| ContextPrecision 低 | 召回掺水，上下文噪声大 | 精排、近义收敛、降低 top-k |
| AbstentionCorrectness 低 | 该拒答却硬答 | 收紧弃答触发、补弃答话术 |
| UncataloguedHandling 低 | 未收录时编数值/夸疗效 | 加强登记话术 + 数值/断言拦截规则 |

> **召回是手段不是目的**。最终看的是生成层（Faithfulness）和端到端体验（用户追问率、可定位率）。只报召回率、不报端到端，是常见的"中间指标自嗨"。

---

## 7. 踩坑清单（本项目实测）

1. **块 id 会变**：文档审核通过后 chunk_id 会变，`expected_id` 对照全失效。审核通过后别改文档，改了要按新 id 重写 `kb_pairs.jsonl`。
2. **不传 `--run-chain` 评的是空回答**：分数好看但没意义。
3. **混评拖低分**：弃答题/未收录题不能和有据题一起算 faithfulness，必须分层（`_categorize` 已处理）。
4. **弃答规则对措辞敏感**：`abstain_markers` 用短词（如"确切依据"）而非长串，因为实际话术中间会插字，长串匹配会漏。
5. **ragas 0.4.x 的 API 变了**：自定义指标评分入口是 `_single_turn_ascore`（不是旧的 `_single_score`），列声明是 `_required_columns = {SingleTurnSample: {...}}`（不是字符串字典）。
6. **`bypass_n=True` 必须开**：DashScope 兼容端点只接受 n=1，不开会 400 Invalid n value。
7. **评估器模型要调大 max_tokens**：Faithfulness 要生成 statement 列表、Relevancy 要生成多个问题，默认 2048 会 LLMDidNotFinish。
8. **评估本身也在花 LLM 钱**：`run.py` 里 `_recording` 会对每次评估调用记账，跑之前心里有数。

---

## 8. 一条完整的最小闭环（照做）

```
① 建集：写 kb_pairs.jsonl（检索）+ kb_qa.jsonl（生成，带 reference 分层）
② 基线：python -m eval.kb_run → 记录 recall + ragas 两组数字 + 当前配置
③ 调优：只改一个变量（如加混合检索 / 开近邻块 / 调 top-k）
④ 复测：同一评估集重跑 kb_run.py
⑤ 对比：相对提升 + 绝对提升，逐条归因到改动
⑥ 验证：holdout 集防过拟合 + 线上反馈交叉验证
```

---

## 9. 面试口径速记

- **"召回率提升 50% 怎么来的？"** → 按 §5 的「基线 → 评估集 → 指标口径 → A/B → 归因」答，并主动说清"相对提升、绝对 +20 个百分点"。
- **"忠诚率是什么？"** → 反幻觉第一指标：回答里每条 claim 能否在检索上下文里找到依据，忠实于检索结果而非模型记忆。
- **"为什么分层评估？"** → 弃答/未收录题的正确答案是拒答，没有 claim，混评会把 faithfulness 拖低，误导结论。
- **"检索好了端到端就一定好吗？"** → 不一定，召回是中间指标，最终看 Faithfulness 和用户追问率/可定位率。
