# plan_v13 评审（第三轮，针对 2026-11-03 12:55 版）

> 对照 [docs/needs/needs_v13.md](../needs/needs_v13.md)（第一～四节 + 4.6 拍板）与 [docs/plan/plan_v13.md](../plan/plan_v13.md)（已按第二轮 review 改过）。
> 这一轮只核对「上轮的 P0/P1/P2 改干净没有、改动本身有没有新问题」。已核对 `kb/publish.py`、`kb/store.py`、`kb/parse_pdf.py`、`kb/milvus.py`、`eval/kb_run.py`、`scripts/gen_kb_samples.py`、`tests/`、`migrations/029_kb.sql`、`.gitignore`。
> 未改代码。日期：2026-11-03。

---

## 1. 结论

**上一轮的 P0 改对了，可以按这版开工。**

`images.new` 那条现在是完整的：写字节到 `images.new/`、`image_path` 记 `images/`、目录换名之后再检查文件存在（§九 step 2/3/4）。顺序对，检查点也对——这是前两轮反复出问题的地方，现在闭环了。

其余：`error` 写回本次告警、验收不看顺序看 span、`--doc-id/--append` 进了文件表、快照落 `data/snapshots/`、remap 用固定排序键，都对。`.gitignore:53` 确实有 `data/snapshots/`，plan 里那句声明我核过了是真的。

剩下的是**一条 P1**（Phase F 的三块新逻辑没有测试条目）和**四条 P2**（都是几行字的事），不影响开工。

| 级别 | 问题 | 位置 |
|---|---|---|
| P1 | §十 的 pytest 清单只覆盖 A～E，F 新增的「双目录 / snapshot / remap / append」一条都没有 | §十 |
| P2 | `--append` 没写去重，重跑一次补题会追加重复题 | §七 |
| P2 | `--limit 2` 撞上「少于 20 字的块直接跳过」，某篇可能出 0 题 | §十 step 2 |
| P2 | 验收没验「重嵌后新图的 URL 能打开」，而这正是两轮 P0 的症状 | §十一 |
| P2 | `dots_mocr` 的 `144` 和 `72/144` 硬编码耦合，改 dpi 就静默错位 | §五 |

---

## 2. 上轮意见的落实情况

| 上轮 | 这一版 | 判定 |
|---|---|---|
| P0 `image_path` 指向 `images.new/`，rename 后悬空 | §九 step 2 写字节到 `images.new/`、路径记 `images/`；`_index` 明确「写到哪」与「记哪条」分开 | 已修 |
| P0 存在性检查在交换之前 | 挪到 step 4，换名之后 | 已修 |
| P1 remap 缺排序键 | `doc_id` → `page_no` 空值在后 → `bbox` 的 y → `text`，并写明「块 id 是随机的，不能当顺序」 | 已修 |
| P1 补题没工具 | `gen_kb_samples` 加 `--doc-id`（可重复）+ `--append`，文件表与 §七 都写了 | 已修 |
| P1 精排验收判据不成立 | §十一 改成看 `kb_rerank` span + 精排分，并写明「顺序相同算通过」 | 已修 |
| P2 重嵌清空 `error` 抹告警 | step 6 改成写回本次告警、不留旧告警 | 已修 |
| P2 dry-run 让 PDF OCR 两遍 | §十 step 4 明确不跑 dry-run，抽图数看重嵌日志 | 已修 |
| P2 `bbox` 语义变化 | §五 写明「从这一版起统一 PDF 点，重嵌前的旧行仍是像素」 | 已修 |
| P2 快照文件会被提交 | 改到 `data/snapshots/`，`.gitignore` 已忽略 | 已修 |

另外两点顺手确认过、没问题：

- **只在 step 2 失败的那篇**（解析/摘要/嵌入失败、旧块没动）在 `--remap` 里是恒等映射：块还是旧 id，快照里的 `(doc_id, block_kind, text)` 原样命中，题不会被映射丢。反过来，若某篇死在后半段（旧块已删、状态待审核），题会被丢掉——这是对的，但日志里要把「丢弃条数」按篇打出来，否则分不清是「映射丢题」还是「重嵌失败」。
- `--remap` 的分组是 `(block_kind, text)`，摘要行（`text` = 摘要）自成一组，不会跟正文块串。旧快照的文本 bbox 是像素、重嵌后是点，但同类内部是等比缩放，相对顺序不变，配对不会错。

---

## 3. P1：Phase F 的三块新逻辑没进测试清单

§十 的 pytest 五条覆盖的是 A（前后文）、B（PDF 插入）、C（两行）、D（去重）、E（精排降级）。F 是本轮改动最大的地方，但验收只有「人工跑一遍脚本」。

风险很具体：**这三块恰好都不会报错地坏掉**。

- `_index` 双目录：如果实现时把 `record_dir` 漏给某一条 row，只有那一条的图 404；
- `--remap`：配错排序键只会让 Recall 悄悄变差，日志里看不出来；
- `--append`：写成覆盖就是把 17 条旧题清空，也没有异常。

建议补三条（都是纯函数级别的，好写）：

1. `_index(doc, warnings, write_dir=tmp/"images.new", record_dir=tmp/"images")`：断言 rows 里每个 `image_path` 都不含 `images.new`，且字节落在 `images.new/` 下；`write_dir == record_dir` 时（`approve` 路径）行为与现在一致。
2. `--remap`：构造两条同 `(doc_id, doc_kind, text)` 的块 + 一条 `text` 不同的块，断言按「第 n 次出现」配对、`text` 对不上的题被丢弃并计数。
3. `--append`：临时 `kb_pairs.jsonl` 里已有 2 条，追加 1 条后仍是 3 条（不是 1 条），且 `kb_qa.jsonl` 未被改写。

---

## 4. P2：四条小口径

**4.1 `--append` 要去重。** §七 只写了「追加到现有 `kb_pairs.jsonl`，不覆盖旧题」。补题命令跑两次就会把同一道题追加两遍，`_recall` 把重复题各算一次，指标会被这一篇带偏。按 `question` 去重即可（重复的直接跳过并打印条数）。

**4.2 `--limit 2` 可能拿到 0 题。** `_gen_one` 对 `len(text) < 20` 的块直接返回 `None` 且不补位（`scripts/gen_kb_samples.py:69-73`）。PDF 的第一块常常是标题行附近，容易不足 20 字。建议 §十 step 2 用 `--limit 5`，或让脚本「凑够 N 条有效题再退出」。这条不改也不会坏，就是那篇可能没题、白导入。

**4.3 验收补一条「重嵌后的新图能打开」。** §十一 最后一条只验「新行能被提问命中」。但这两轮 P0 的症状都是「文本命中、图片 404」，人眼最容易漏。加一句：用重嵌后的新 `chunk_id` 请求 `/kb/images/{chunk_id}?tenant_id=...`，返回 200 且是正确那张图。

**4.4 `144` 别散在代码里。** §五 写「乘 `72/144`」，但 `144` 来自 `parse_pdf.py` 里的 `page.get_pixmap(dpi=144)`。两处硬编码一旦有一处改了 dpi，坐标就静默错位（图插到页首或页尾，不报错）。建议提成模块级常量（如 `_RENDER_DPI = 144`），换算写 `72 / _RENDER_DPI`。

---

## 5. 结论（复述）

这版可以开工，不用再改结构。上一轮那个 P0（图片路径与检查顺序）已经闭环，其余意见也都落到了具体句子。

开工前建议只补两件事：**§十 的 pytest 清单加上 Phase F 的三条用例**（双目录、remap 对齐、append 不覆盖），以及 §十一 验收补「新图 URL 返回 200」。四条 P2 顺手写进去即可，不影响排期。
