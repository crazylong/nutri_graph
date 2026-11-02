# code_review_v11 —— plan_v11 实现代码评审

> 对照 [`docs/plan/plan_v11.md`](../plan/plan_v11.md)（第七节 Phase D 是流式施工口径）与 [`docs/review/review_v11.md`](../review/review_v11.md)（五轮评审结论）。
> 评审文件：`kb/memory.py`、`kb/streamer.py`、`kb/workflow.py`、`api/kb.py`、`kb-web/src/App.vue`、`tests/test_kb_ask.py`。
> 未改代码。日期：2026-11-02。
> §一 是结论，§二 是五轮评审结论的落实核对，§三 是发现的观察点（非阻塞），§四 是测试质量评价。

---

## 1. 结论

**实现质量高，可以验收。** 五轮评审的每一处结论都正确落地，后端流式链路、前端单气泡、取消语义、信号量释放、`sid` 回传全部按方案实现，逻辑自洽，测试覆盖充分。

发现的问题都是**非阻塞**的观察点（见 §三），没有一个会导致验收失败。核心流式路径（`KbStreamer` 自建推送对象 + `_generate` 的 `hook()` 分支 + `_run_kb_stream` 三支异常收尾 + `_sse_with_cancel` 打取消标记）与 plan 完全一致。

---

## 2. 五轮评审结论的落实核对

| 评审结论（累计 15 条） | 落点 | 判定 |
|---|---|---|
| ① 流式成功后 `memory.remember`，取消不落库 | `api/kb.py:175-179` | ✅ `if sid and not cancelled()` 后 `try/except pass` |
| ② 不复用 `Live`，自建 `KbStreamer` | `kb/streamer.py` 整文件 | ✅ 无 `rewrite_answer`，分句条件同 `Live.feed` |
| ③ 流式开关看 `hook()`、不进 state、请求用表单 | `kb/workflow.py:108`、`api/kb.py:208-213` | ✅ `hook() is not None` 分支；`Form`+`File` |
| ④ 前端单气泡 | `App.vue:214-215` 一次 push user+assistant | ✅ |
| ⑤ `KbStreamer.flush()` 刷句末残留 | `kb/streamer.py:62-66` | ✅ 只推 `pending`，不改写 |
| ⑥ 断开自己打取消标记 | `api/kb.py:127-134` `_sse_with_cancel` | ✅ `finally` 里 `bus.cancel` |
| ⑦ D2 不传 `stream=` | `api/kb.py:163-171` | ✅ `with bind(streamer): run_ask(...)` |
| ⑧ 发送后清图片 | `App.vue:211-212` | ✅ 在 `data.set('image')` 之后清 `file` + 复位 input |
| ⑨ `_sse_with_cancel` 用 `async for` | `api/kb.py:130-132` | ✅ |
| ⑩ 工作线程释放信号量 + 三支异常收尾 | `api/kb.py:189-200` | ✅ `except` 分三支 + 最外层 `finally` release |
| ⑪ 取消两条路径 + 清图片顺序 | `api/kb.py:175`、`App.vue:210-212` | ✅ |
| ⑫ `result.session_id` 用 `sid` | `api/kb.py:185-186` `if sid: payload["session_id"] = sid` | ✅ |
| ⑬ `text_delta` 置 `sent_text` | `kb/streamer.py:35-36` | ✅ 推出非空正文才置位 |
| ⑭ 取消后 `error`/`done` 仍发出 | `kb/streamer.py:24-27` | ✅ `event in ("error","done")` 不抛 |
| ⑮ 前端 ref + localStorage 都写 | `App.vue:245-248` | ✅ 两者都写 |

**结论：15 条全部落实，无遗漏。**

---

## 3. 观察点（非阻塞，供后续参考）

以下都不影响验收，但实现时值得留意：

### 3.1 取消的窄窗口：`remember` 之后、`result` 之前

`_run_kb_stream` 的 `work()` 顺序是 `flush → 补整段 → remember（带 cancelled 检查）→ result → finish_stream`。若取消标记打在 `remember` 之后、`result` 之前，这一轮已经落库、但 `result` 事件会抛 `StreamCancelled` 走 `error` 分支。

这不是 bug：plan D4 明确只承诺「模型已返回、`remember` 还没调用时靠 `remember` 前查一次 `cancelled()` 拦住」，这个窗口极窄、且落库的结果用户重新打开会话能看到，语义可接受。若要彻底堵死，可在 `remember` 之后再补一次 `cancelled()` 检查并回滚——但本期不做，plan 也不要求。

### 3.2 `_kb_slots` 全局状态跨测试共享

`api/kb.py:104-110` 的 `_kb_slot()` 用模块级 `_kb_slots` / `_kb_slots_n` 缓存，`_kb_slots_n != n` 时才重建。这保证了 `config.STREAM_MAX_CONCURRENT` 变化后能重建信号量（`test_stream_model_failure_finishes_and_releases_slot` 里 `monkeypatch.setattr(config, "STREAM_MAX_CONCURRENT", 1)` 正是依赖这个检测）。

隐患：monkeypatch 恢复 config 后，`_kb_slots` 的旧 Semaphore 对象不会自动恢复，跨测试共享。因为每次 `_kb_slot()` 都会重新比对 `n`，功能上正确，只是测试间有轻微状态残留。不影响验收。

### 3.3 `_prepare_ask` 里 `check_tenant` 未单独捕获

`api/kb.py:118` `tenant = check_tenant(tenant)` 直接抛 `KbError`，靠 `ask_stream` 调用处（`api/kb.py:219`）的 `except KbError` 统一捕获返回 `_fail`。这与非流式 `ask()`（`api/kb.py:52-54` 内层 try/except）行为一致，只是捕获位置不同，结果相同。无问题。

### 3.4 流式 `ask_stream` 先读图再校验租户

`api/kb.py:215` 先 `image.read()`，之后 `_prepare_ask` 才 `check_tenant`。租户非法时图已读进内存（一次性开销，无泄漏），行为可接受。若在意，可把 `check_tenant` 提到 `image.read()` 之前——非必要。

### 3.5 假 `stream_model` 不经过真实 `feed` 路径

`test_stream_emits_deltas_and_saves_hits` 的 `fake_stream` 直接返回字符串、不调 `feed`，所以该用例走的是「`sent_text` 为假 → 补整段」路径，而非真实的逐 chunk `feed` 路径。真实 `feed` 路径由 `test_streamer_flushes_unpunctuated_tail` 单独覆盖。两条路径各自有测，合起来覆盖完整，属合理的测试拆分，不是遗漏。

---

## 4. 测试质量评价

`tests/test_kb_ask.py` 新增/扩展的用例与 plan 第九节的 9 个落点一一对应：

| plan 落点 | 用例 | 判定 |
|---|---|---|
| 会话列表 + 非法 client_id | `test_session_list_orders_by_last_active_and_rejects_bad_client` | ✅ 倒序 + 400 + 标题 |
| 最近问过 | `test_recent_questions_are_newest_first` | ✅ 倒序 |
| 会话隔离 | `test_sessions_stay_inside_one_client` | ✅ `kb:` 前缀隔离 |
| 多轮 history 拼装 | `test_later_turn_keeps_recent_dialogue_and_image`（非流式） | ✅ 第二问 prompt 含上文 |
| 流式端点 | `test_stream_emits_deltas_and_saves_hits` | ✅ text_delta + result + 落库 + 续传 |
| 流式落库 | 同上 | ✅ hits 回放 |
| 取消不落库 | `test_stream_cancel_does_not_save_messages` | ✅ 空会话、无消息 |
| 句末 flush | `test_streamer_flushes_unpunctuated_tail` | ✅ 无标点尾段刷出、不重复 |
| 模型失败收尾 | `test_stream_model_failure_finishes_and_releases_slot` | ✅ error+done + 信号量释放后能再发 |

测试用 `MemoryBus` + monkeypatch `stream_model`/`invoke_model`/`search_question`，不打真 Redis、不调真模型、不碰 Milvus/DashScope，符合 plan「假实现替换」的要求。取消用例里假 `stream_model` 停在 `cancelled()` 变真之前再抛 `StreamCancelled`（`tests/test_kb_ask.py:291-295`），正确处理了「cancel 晚于 remember 会导致断言时好时坏」的隐患。

---

## 5. 结论（重申）

可以验收。五轮评审的 15 条结论全部落实，测试覆盖 9 个落点，发现的 5 个观察点均为非阻塞。建议下一步在服务器上按 plan 第八节跑 `pytest tests/test_kb_ask.py -q` 和手工路径验证。
