# review_v15 —— needs_v15 对 plan_v15 的评审

> 评审时间：2026-11-04
> 对象：`docs/needs/needs_v15.md` → `docs/plan/plan_v15.md`
> 结论：**评审通过，可以施工。**

---

## 总体结论

plan_v15 把 needs_v15 的每一项都落到了具体文件和函数上，第二节、第三节的口径全部对齐，没有偏离需求自创范围。四个 Phase 划分合理，人日估算（0.9 + 0.3 测试）务实。

---

## 逐条对照（需求 → 方案）

| needs 要求 | plan 落地 | 判定 |
|---|---|---|
| 召回仍约 20 条，不改 60 | 二期不做清单 + 不动 `kb/search.py`/`milvus.py` | ✅ |
| 收敛后按序号读，不二次搜索 | Phase A `store.py` 按 `doc_id, seq` 读，不嵌向量 | ✅ |
| 只补同章节（`heading_path` 相同） | SQL 查询一层 + `neighbor.py` 再过滤一层（双保险） | ✅ |
| 邻居不参与精排/收敛 | C 阶段明确「`_diversify` 之后调用」 | ✅ |
| 顺序：前块、命中、后块 | B 阶段步骤 1→2→3 | ✅ |
| 重叠邻居只出现一次 | B 阶段「同一 `chunk_id` 先出现的位置保留」 | ✅ |
| 邻居进 `hits` 带 `neighbor:true`，无分，不占名额 | C 阶段 + `run_ask` 组装 | ✅ |
| 图片提问、评测不补 | C 阶段只在文本分支 + 二期不做 `eval/kb_run` | ✅ |
| 旧块无序号不补、不全库重嵌 | A 阶段「已有行保持 NULL」+ D 阶段用例 | ✅ |

---

## 实现一致性核查（已读源码）

1. **函数名/接口全部真实存在**：`replace_chunks`（store.py:98）、`_index`/`_image_rows`（publish.py）、`_retrieve`/`_diversify`/`_generate`/`run_ask`（workflow.py）、`format_hit`（common.py:40）。plan 引用的名字一个没编造。

2. **一张图两行的结构吻合**：`_image_rows` 确实返回 `[image_row, summary]` 两行且共用 `image_path`（publish.py:246-253），与 needs「图像行和描述行序号紧挨、正文相同、命中一行时另一行不当邻居」完全对应。plan 用 `image_path` 相同来跳过另一行，判断正确。

3. **`replace_chunks` 是「整篇删再插」**：plan 说「重嵌仍是整篇删除再插入，序号跟新块一起重编」——与 store.py:100 的 `DELETE FROM kb_chunk WHERE doc_id` 一致，序号从 0 重编无残留风险。

4. **`run_ask` 当前组装 `score` 是硬取值**（workflow.py:55 `hit["score"]`）：needs 要求邻居「不写检索分」，plan C 阶段写「`score` 允许为空」——但现有代码 `hit["score"]` 若邻居 dict 里根本没有 `score` 键会 `KeyError`。**施工时必须改成 `hit.get("score")`**。

5. **`_images` 用 `block_kind == "image"` 挑图**（workflow.py:134）：邻居块若是图像行，也会被 `_images` 抓走。但文本提问链路 `_images` 阶段 `image` 输入为空、不走 `search_picture`，邻居图行进来只会被 `_generate` 的 `format_hit` 当文本用。与 needs 2.4「邻居若自己有图，仍按章节放进对应条目」不冲突。

---

## 值得肯定的设计点

- **双保险过滤 `heading_path`**（查询层 + 拼接层）——防调用方误传，符合需求 2.2 的严谨性。
- **try/except 兜底不 500**——与现有 `_rerank`/`_diversify` 的容错风格一致，`log.warning` 措辞也定了。
- **`conftest` 不关开关**：让老用例在「序号为空 → 补齐无效果」下自然通过，避免破坏现有「返回 6 条」断言。
- **迁移只加可空列 + 唯一索引**：NULL 并存（Postgres 唯一索引放过 NULL），零数据迁移、不重嵌，完全符合 needs 2.3。

---

## 施工时需留意的 3 个小点

1. **`run_ask` 的 `score` 取值**：必须改成 `hit.get("score")`，否则邻居无分字段会 `KeyError`。这是唯一可能直接引发运行时崩溃的地方。
2. **`run_ask` 组装 `hits` 时 `neighbor` 字段透传**：现有组装 dict 只有 6 个固定键（chunk_id/block_kind/text/heading_path/score + 可选 image_url），要把 `neighbor: true` 透传出去，需加 `if hit.get("neighbor"): item["neighbor"] = True`。
3. **邻居 `text` 必须有值**：`_generate` 里 `format_hit(hit)` 吃 `hit["text"]`，B 阶段已要求邻居行带 `text`，SQL 要确保 SELECT 出 `text` 列（plan Phase A 字段清单已含 `text`，OK）。
