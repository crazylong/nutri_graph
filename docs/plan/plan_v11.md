# plan_v11 —— 企业知识库问答页多轮对话 + 历史会话对齐

> 生成时间：2026-11-02
> 上游：[`docs/needs/needs_v11.md`](../needs/needs_v11.md)。第二节是需求，第三节是评审口径。
> 与第二节原文冲突时，按第三节 3.3 施工。这三点已经写进下面正文，不要再回 needs 里抄：
> 不碰 `short_term.py`；会话列表按 `client_id`、不按租户；流式复用总线（`chatbus`/`iter_sse`）和 `stream_model`，推送对象自建（`KbStreamer`）、不复用 `Live`。
> 2026-11-02 又按 [`review_v11`](../review/review_v11.md) 的复核改，复核结论以第七节正文为准：
> 第一轮（① 流式工作线程成功后要 `memory.remember`、取消时不落库；② 不复用 `Live`，kb 自建推送对象；③ 流式开关看 `hook()`、不进 state，请求继续用表单；④ 前端只保留一条回答气泡）。
> 第二轮（⑤ `KbStreamer` 要加 `flush()` 刷句末残留，不靠「没推过字才补整段」；⑥ 断开要自己打取消标记——`bus.cancel()` 全仓库无调用方，`api/kb.py` 外包一层生成器在 `finally` 里 `bus.cancel`，`remember` 前再查一次 `cancelled()`；⑦ D2 不再传 `stream=`，工作线程只 `with bind(...)` 调 `run_ask`；⑧ 前端发送后清掉 `file`，避免下一轮误走识图）。
> 第三轮（⑨ `_sse_with_cancel` 用 `async for`，不能 `yield from`（`iter_sse` 是 async def）；⑩ 工作线程要释放信号量、三支 `except` 都发 `error`+`done`（模型池耗尽不是 `StreamCancelled`）；⑪ 取消两条路径都算取消、清图片放在放进表单之后）。
> 第四轮（⑫ `result` 的 `session_id` 用 `_session()` 返回的 `sid`，不用表单 `session_id`（新对话表单是空串，用了首轮就断多轮）；⑬ `KbStreamer.text_delta` 自己置 `sent_text`；⑭ 取消后 `error`/`done` 仍要发出，不抛 `StreamCancelled`）。
> 第五轮（⑮ 前端 `result.session_id` 要同时写 `sessionId.value`（ref）和 `localStorage`，下一轮组表单读的是 ref；D2 草图 `payload["session_id"] = sid` 加 `if sid:` 守卫，与「空 sid 不写」对齐）。
> 性质：只写怎么施工。主站食疗问答链路、`short_term.py` 本体、`kb/workflow.py` 的既有检索逻辑不动。

---

## 一、范围

| Phase | 内容 | 大致人日 |
|---|---|---|
| A | 后端补两个薄接口：`GET /kb/sessions`、`GET /kb/recent` | 0.3 |
| B | kb-web 前端加左侧栏（新对话 + 历史会话 + 最近问过） | 0.8 |
| C | 多轮对话 + 上下文优化回归验收（含压缩触发链路确认） | 0.5 |
| D | 流式回答：`/kb/ask/stream` + `_generate` 走 `stream_model` + 前端 SSE | 1.3 |
| **合计（不含服务器验证）** | | **≈ 2.9 人日** |
| 服务器上跑测试、kb-web 构建部署 | | 0.4 |

**顺序：A → B →（C 与 D 并行）→ 联调。** B 依赖 A 的接口。C 是回归 + 配置口径核对。D 的流式端点依赖 `run_ask`（C 会先确认它不回归），D 的前端 SSE 依赖 D 的端点。

---

## 二、本期不做

登录/手机号验证码、会话删除/重命名、按租户隔离历史、摘要 prompt 四段式（kb 保持三段式）、`short_term.py` 任何改动。

---

## 三、要动的现有文件

| 位置 | 改什么 |
|---|---|
| `kb/memory.py` | 加 `list_sessions(client_id)`、`recent_questions(client_id)` 两个薄封装，内部调 `ShortTermMemory` 对应方法，user_id 走 `user_id(client_id)` |
| `api/kb.py` | 加 `GET /kb/sessions?client_id=`、`GET /kb/recent?client_id=` 两个路由；加 `POST /kb/ask/stream` 流式端点 |
| `kb/workflow.py` | `_generate` 增加流式分支：`hook()` 有目标时调 `stream_model`（返回值当 answer），否则仍 `invoke_model` |
| `kb/streamer.py`（新增） | kb 自己的流式推送对象 `KbStreamer`：只按句推、不调 `rewrite_answer`（分句条件同 `Live.feed`），带 `flush()` 刷句末残留，`feed` 返回 True |
| `kb-web/src/App.vue` | 加左侧栏（新对话 + 历史会话列表 + 最近问过），补 `loadHistory`/`openSession`/`startNewChat` 的 Vue 版逻辑；`ask()` 改走 SSE 流式（单气泡） |
| `kb-web/src/main.js` | 无（App.vue 已挂载）。若侧栏需要抽组件，新增 `kb-web/src/components/Sidebar.vue`（可选） |

**不动**：`memory/short_term.py`、`kb/search.py`、`kb/memory.py` 已有的 `open_session`/`history_of`/`remember`/`messages`、`chatbus.py`、`graph/live.py`、`llm/agent.py`、`llm/streamctx.py`、`kg/sensitive.py`、`frontend/`、`admin-web/`。

---

## 四、Phase A：后端两个薄接口

### A1 · `kb/memory.py` 补封装

在现有文件末尾追加两个函数（复用 `ShortTermMemory` 已有方法，不加新 SQL）：

```python
def list_sessions(client_id: str) -> list[dict]:
    """client_id 名下的会话列表，按最后活跃时间倒序。字段与主站 /sessions 一致。"""
    return ShortTermMemory().list_sessions(user_id(client_id))


def recent_questions(client_id: str, limit: int = 20) -> list[dict]:
    """kb 侧最近问过的原话。点一下是再问一句，不是打开旧会话。"""
    return ShortTermMemory().recent_questions(user_id(client_id), limit=limit)
```

`user_id(client_id)` 已存在（`kb/memory.py:33`），加 `kb:` 前缀，物理隔离主站会话。

### A2 · `api/kb.py` 加路由

在 `session_messages`（`/kb/sessions/{session_id}`）附近加两个 GET。**注意路由顺序**：`/kb/sessions`（列表）与 `/kb/sessions/{session_id}`（详情）不冲突（FastAPI 按字面段区分），但写法上列表放详情前面更清晰，避免 `session_id` 匹配到「sessions」字面量。

```python
@router.get("/kb/sessions")
def kb_sessions(client_id: str = ""):
    try:
        cid = memory.check_client(client_id)
    except KbError as exc:
        return _fail(exc)
    return {"sessions": memory.list_sessions(cid)}


@router.get("/kb/recent")
def kb_recent(client_id: str = ""):
    try:
        cid = memory.check_client(client_id)
    except KbError as exc:
        return _fail(exc)
    return {"questions": memory.recent_questions(cid)}
```

`check_client` 在 `kb/memory.py:26`（正则 `^[A-Za-z0-9_-]{8,64}$`），`api/kb.py` 现在只 `from kb import memory`，没有裸 `check_client`，所以要写 `memory.check_client`。`KbError` **已经在** `kb.common` 的 import 里（`api/kb.py:10` 是 `from kb.common import ABSTAIN, KbError, check_tenant`），不用再补。保证 `client_id` 合法再拼 `kb:` 前缀，避免非法字符串进 `ShortTermMemory`。

**鉴权口径**：与现有 `/kb/sessions/{id}` 一致，走 `client_id` 查询参数，不做登录态（v10 已定 kb 不登录）。`list_sessions`/`recent_questions` 内部按 `user_id` 过滤，天然只返回该 `client_id` 名下的数据。

---

## 五、Phase B：kb-web 前端左侧栏

### B1 · 布局

`App.vue` 的 `.page` 改为左右两栏：左侧 `aside.sidebar`（固定宽约 260px，窄屏收起或置顶），右侧主线程区。侧栏内容：

1. **新对话**按钮（顶部）→ `fresh()`。
2. **历史会话**列表：标题 + 最后活跃时间，点击 `openSession(id)`，当前会话高亮 `active`。
3. **最近问过**列表：原话，点击是「再问一句」（带租户时走 `ask`，不带租户先提示选租户）。

### B2 · 数据流（对齐主站 app.js）

新增组合式状态与函数：

```js
const sessions = ref([])
const recent = ref([])

async function loadHistory() {
  if (!clientId()) return
  const [rs, rq] = await Promise.all([
    fetch('/kb/sessions?client_id=' + encodeURIComponent(clientId())),
    fetch('/kb/recent?client_id=' + encodeURIComponent(clientId())),
  ])
  if (rs.ok) sessions.value = (await rs.json()).sessions || []
  if (rq.ok) recent.value = (await rq.json()).questions || []
}

async function openSession(id) {
  const r = await fetch('/kb/sessions/' + encodeURIComponent(id) + '?client_id=' + encodeURIComponent(clientId()))
  if (!r.ok) { fresh(); return }
  const body = await r.json()
  sessionId.value = id
  localStorage.setItem(SESSION_KEY, id)
  turns.value = body.messages || []
  loadHistory()
}

function startNewChat() {
  sessionId.value = ''
  turns.value = []
  localStorage.removeItem(SESSION_KEY)
  loadHistory()
}
```

**关键对齐点（主站已有、kb 要补）**：

1. **会话列表在每次提问后刷新**：主站 `ask` 成功后调 `loadHistory()`（app.js:1469）。kb 的 `ask()` 成功后也要 `loadHistory()`，否则侧栏不更新（新会话标题、活跃时间）。
2. **打开旧会话后续传**：`openSession` 写入 `sessionId.value` + localStorage，后续 `ask` 里 `if (sessionId.value) data.set('session_id', sessionId.value)`（App.vue 已有），即续传。
3. **「最近问过」点一下再问**：主站 `renderRecent` 的点击是 `ask(row.question)`。kb 里点最近问过 → 若未选租户先提示，否则把 `question.value = row.question` 并调 `ask()`。
4. **新对话不删库**：`fresh`/`startNewChat` 只清 localStorage 的 session key 和当前线程，不调删除接口（主站同样，`startNewChat` 只 `removeItem(K_SID)`）。旧会话仍留在侧栏可重新打开。

### B3 · 现有 `fresh()` 复用

`App.vue` 已有 `fresh()`（清 sessionId + turns + 移除 SESSION_KEY），`startNewChat` 即其别名（或直接在按钮绑 `fresh` 并补 `loadHistory()`）。避免重复定义两套。

### B4 · 样式

沿用 App.vue 现有的 `--ng-*` 设计变量，侧栏用白底卡片 + 阴影，与主线程区分。历史项 hover 高亮、当前项 `active` 用 `--ng-accent-weak`。窄屏（< 720px）侧栏改为顶部横向收起或折叠，不做复杂响应式，够演示即可。

---

## 六、Phase C：回归验收 + 上下文优化口径核对

### C1 · 上下文优化链路核对（不改代码，只确认）

`kb/memory.py` 已接入滚动摘要：`remember` → `save_round` → `_defer_compress` → `compress_if_needed(summarize_fn=_kb_summarize)`。核对两处配置口径，若未开则补进服务器 `.env`（**不写死代码默认值**）：

- `MEMORY_ENABLED`：决定 `history_of`/`remember` 是否落库拼上下文。kb 多轮对话依赖它，必须 `true`。
- `MEMORY_SUMMARY_ENABLED`：决定是否做滚动摘要。`false` 时只有「最近 N 轮原文」，没有更早轮次摘要（`load_context` 里 summary 为空，但第三段「未盖住更早原文」仍会拼，长会话上下文会变长但不丢）。
- `MEMORY_RETAIN_TURNS`：保留最近几轮原文（主站口径，kb 沿用）。

**验收口径**：连续问超过 `MEMORY_RETAIN_TURNS` 轮后，第 1 轮的内容仍能通过摘要被后续回答理解（抽查指代），且摘要落库（`session.summary` 非空、`summary_until_msg_id` 推进）。

### C2 · 多轮对话回归

同一 `client_id` 连续问：

1. 「《员工手册》里考勤制度是什么？」→ 有据回答；
2. 「那加班费怎么算？」→ 能理解「那」指上文，命中考勤/加班相关块。

不新增代码，只确认 `kb/workflow.py` 的 history 拼装仍生效。

### C3 · 历史会话回归

1. 问完几轮 → 侧栏出现该会话（标题 = 首问前 40 字，时间 = 最后活跃）；
2. 点「新对话」→ 线程清空，侧栏旧会话仍在；
3. 点旧会话 → 恢复全部消息（含命中的图块 `hits`）；
4. 恢复后继续问 → 新轮次追加，`session_id` 续传（页面走流式，`/kb/ask/stream` 表单带旧 session_id；非流式续传留在 pytest）；
5. 「最近问过」点一下 → 再问一句（需已选租户）。

---

## 七、Phase D：流式回答

> 复用主站流式设施（`chatbus`/`iter_sse`/`stream_model`/`spawn`/`bind`），**不复用 `graph/live.py` 的 `Live`**——它绑定食疗敏感改写，会把制度回答里的「治疗/诊断」删句或换成食材话术（见 D1）。改动集中三处：`kb/workflow.py` 生成节点、`api/kb.py` 流式端点、`kb-web` 前端 SSE。

### D1 · 生成节点走流式，但**自建推送对象，不用 `Live`**

现在 `_generate` 用 `invoke_model`（`llm/agent.py:42`，一次性返回）。流式分支改调 `stream_model`（`llm/agent.py:104`），逐 chunk 经一个**kb 自己的推送对象**按句推。

**为什么不复用 `Live`**：`graph/live.py` 的 `feed → _apply → _rewrite` 每次都调 `kg.sensitive.rewrite_answer`，且**不看 `SENSITIVE_OUTPUT_SCAN`**（那个开关只包住主站同步路径，`graph/nodes.py:727`）。改写会：政治类整段换 `POLITICAL_ANSWER`；疗效类整句删掉，删光换 `MEDICAL_FALLBACK`（「这个我只能按图谱里收录的食材信息来回答……」）。员工手册、工伤、医疗报销里很容易出现「治疗」「诊断」，kb 现在 `_generate` 是把 `invoke_model` 的 `content` 原样当答案，没有这层改写——引入 `Live` 会把制度回答改坏。

**施工口径**：

- `chatbus`、`iter_sse`、`spawn`、`stream_model`、`bind` 照复用。
- `graph/live.py` **不动**。kb 在 `kb/` 下新建一个**只按句推送、不调 `rewrite_answer`** 的对象（例如 `kb/streamer.py` 的 `KbStreamer`），分句条件与 `Live.feed` 相同（`。！？；\n`、满 80 字、或 2 秒），实现 `feed` / `cancelled` / `status` / `text_delta` / `result` / `finish_stream`，不实现 `_rewrite`。
- **`KbStreamer` 要加 `flush()`**：`feed` 把未碰到句读、不满 80 字、且距上次推送不足 2 秒的文字留在 `pending`（`Live` 同理，`graph/live.py:101-114`），模型结束、最后一段又没标点时这段会一直留在缓存里。主站靠 `Live.finish()`（`graph/live.py:116-120`）推掉。`KbStreamer` 不做 `finish`/`align`（那些带改写/替换语义），但**必须有一个 `flush()`**：只把 `pending` 里剩余的**原文** `text_delta` 出去，不改写、不 `answer_replace`、不拿它当答案。`run_ask` 正常返回后先 `flush()`，再走「没推过字则补弃答句」。
- **`feed` 要返回 `True`**：`stream_model` 里 `feed(piece) is False` 会停止继续推送（`llm/agent.py:99`），返回值仍当成功正文。
- **`text_delta` 要自己置 `sent_text`**：`Live.emit` 里 `event == "text_delta"` 时把 `sent_text` 置真（`graph/live.py:50-51`），`KbStreamer` 不复用 `Live`、要自己写。`feed` 里已经按句推出的走 `text_delta`，也走这条。否则整段停在 `pending` 的短回答会先被 `flush` 推一遍，紧接着「没推过字则补整段」再推一遍，屏幕出现两遍。
- **取消后 `error` / `done` 仍要发出**：`Live.emit` 在已取消时，除了 `error` 和 `done` 都会抛 `StreamCancelled`（`graph/live.py:40-41`），`KbStreamer` 不复用 `Live`、要自己保留这条例外。异常分支里的 `error` + `finish_stream`（`done`）若再抛 `StreamCancelled`，异常会逃出 `except`、`done` 发不出去、SSE 干等到超时。所以 `text_delta` / `result` / `status` 在已取消时可以抛（用来打断生成），`error` 和 `done` **不可以**。
- `_generate` 的流式分支把 `stream_model(...)` 的**返回值**当 `answer`（`stream_model` 返回的是 `_read_stream` 拼的模型原文），与现在读 `invoke_model` 的 `content` 同一口径。**不要**用 `finish()`/`align()` 的结果落库——`feed` 推到页面的字和落库的答案必须都来自模型原文，避免「屏幕上是食疗话术、库里是原文」的分裂。

**流式开关不进 state，看 `hook()`**：`AskState`（`kb/workflow.py:15-24`）没有流式字段，`llm/streamctx.py` 开头注释写明「没声明的键会被图丢掉」，推送对象不能放进 state。`_generate` 用 `hook()` 分支——有目标就走 `stream_model`，没有仍走 `invoke_model`。这与主站 `_complete_from_prompt` 一致。`bind` 写在工作线程内部（`utils/spawn` 用 `copy_context`，但和主站放在同一位置，取消标志的生命周期才清楚）。

**非流式路径必须继续调 `invoke_model`**：现有 `tests/test_kb_ask.py` 的 `test_later_turn_keeps_recent_dialogue_and_image` 替换的是 `kb.workflow.invoke_model`，非流式分支不调它这组回归就断。

### D2 · `api/kb.py` 加 `/kb/ask/stream`

照 `api/chat.py` 的 `chat_stream`/`_run_stream` 模式，换成 kb 的入参和 payload。**请求继续用 `Form` + `File`**（kb 有图片，不能照抄主站的 JSON body；前端只抄 `readSse` 的拆帧，`ask()` 仍提交 `FormData`）。

```python
@router.post("/kb/ask/stream")
async def ask_stream(
    tenant_id: str = Form(""),
    question: str = Form(""),
    client_id: str = Form(""),
    session_id: str = Form(""),
    image: UploadFile | None = File(None),
):
    # 1. 请求线程里做与 ask() 相同的入参校验、读图、_session()
    #    （已有会话要在检索前拿到 history）
    # 2. open_bus()；None 则 503「流式通道不可用」
    # 3. slots 信号量 acquire；满则 503 stream_busy
    # 4. spawn 后台线程 _run_kb_stream（release_slot=True）：
    #      try:
    #          kb_streamer.status("正在查找")          # 状态事件来源
    #          with bind(kb_streamer):
    #              result = run_ask(...)              # 不传 stream=；hook() 在 _generate 里读
    #          kb_streamer.flush()                    # 只刷 pending 剩余原文，不套门槛
    #          if not kb_streamer.sent_text:          # 没推过字（弃答/极短）补整段
    #              kb_streamer.text_delta(result["answer"])
    #          if sid and not kb_streamer.cancelled():# 空 sid 不落库；断开窗口再查一次
    #              try:
    #                  memory.remember(sid, spoken, result["answer"], result["hits"])
    #              except Exception: pass             # 落库失败吞掉，仍发 result
    #          if sid: payload["session_id"] = sid    # 用 _session() 返回的 sid；空 sid 不放这个字段
    #          kb_streamer.result(payload)
    #          kb_streamer.finish_stream()
    #      except (StreamCancelled, ModelStreamBroken):
    #          kb_streamer.error("生成中断")；kb_streamer.finish_stream()  # 不 remember
    #      except Exception:
    #          kb_streamer.error("生成失败")；kb_streamer.finish_stream()  # 不 remember
    #      finally:
    #          if release_slot: _kb_slots().release()  # 最外层释放，防止连问超 8 次全 503
    # 5. StreamingResponse(_sse_with_cancel(bus, stream_id, deadline), media_type="text/event-stream")
    #    （spawn 抛错时请求线程也要放锁）
```

**复用点与关键差异**：

- `chatbus.open_bus()`、`chatbus.iter_sse()`、`chatbus.stream_limit_seconds()` 通用，直接 import。响应头照抄 `chat.py` 的 `Cache-Control: no-cache` + `X-Accel-Buffering: no`。
- 并发信号量**独立一份 `_kb_slots()`**（`api/chat.py` 的 `_slots()` 是模块私有，不要共用；读同一个 `STREAM_MAX_CONCURRENT`，默认 8，两套额度互不占用）。**工作线程最外层 `finally` 释放**（照 `api/chat.py:194-196`），`spawn` 抛错时请求线程再放一次（照 `api/chat.py:222-226`），否则连问超过 8 次全部 503。
- **工作线程要收尾、不能裸死**：`stream_model` 一个字没推出就失败时抛的是模型池耗尽（`plan.exhausted()`），不是 `StreamCancelled`/`ModelStreamBroken`。只捕这两种会让线程直接死掉，SSE 干等到 `stream_limit_seconds`，前端停在「正在查找」。所以 `except` 分三支：`StreamCancelled`/`ModelStreamBroken` 发 `error`（生成中断）+ `done`；其它异常发 `error`（生成失败）+ `done`；都不 `remember`。
- `spawn`（`utils.spawn`）照搬。
- **`run_ask` 不传 `stream=`**：`run_ask` 今天没有这个参数，加了再塞进 `invoke` 的字典会被图丢掉。工作线程只做 `with bind(kb_streamer): result = run_ask(...)`，签名保持现在的关键字参数。`_generate` 里靠 `hook()` 分支（见 D1）。
- **`spoken` 要定义**：与 `api/kb.py:69` 相同，`text or name or "（上传图片）"`。图片轮若用空问句，`save_round` 看到空标题就不写 `session.title`，侧栏变未命名。
- **`remember` 失败要吞异常**（照 `/kb/ask` 约 71-73 行）：只捕 `StreamCancelled`/`ModelStreamBroken` 的话，落库失败会把已流完的回答报成错误，前端拿不到 `session_id`。所以 `remember` 单独 `try/except pass`，之后仍发 `result` + `session_id`。
- **空 `sid` 不落库**：没带 `client_id` 时 `_session` 返回空 `sid`，与 `api/kb.py` 的 `ask` 一样，空 `sid` 不调用 `remember`、`result` 里也不写 `session_id`。
- **`result` 的 `session_id` 用 `sid`，不是表单字段**：`ask_stream` 的参数 `session_id` 是表单原值，新对话时前端不带、是空串。`_session()` 新建会话返回的新 id 才是 `sid`（`api/kb.py:68-74` 只有它非空才写 `result["session_id"] = sid`）。若按字面用 `payload + session_id`，首轮 `result.session_id` 为空 → 前端不写 localStorage → 第二轮再开新会话，指代和侧栏续传对不上。所以 `payload["session_id"] = sid`；`sid` 为空时不放这个字段；有 `sid` 时即便 `remember` 失败也要把 `sid` 放进 `result`（会话行已由 `open_session` 建好，下一轮还能续上）。
- **`status("正在查找")` 在 `run_ask` 之前发一次**：`stream_model` 自己不发 `status`，D4 第 1 条的状态事件要有来源。
- **工作线程里必须 `memory.remember`**：前端改走流式后，这条是唯一的落库入口。漏了它，`history_of` 下一轮没上文（KBQ-01 指代验收失败）、`list_sessions` 没标题、`GET /kb/sessions/{id}` 回放不到命中块。**弃答也落库**（和 `/kb/ask` 一致）。`remember` 放在 `with bind` **外面**：后台摘要 `_defer_compress` 拷走的 context 里没有推送对象，摘要不会灌进回答气泡。
- **`mark_skip_save` 不能代替 `remember`**：主站能用它，是因为落库在图内的 `memory_write_node` 且先看 `skip_save()`（`graph/nodes.py:838-842`），整个调用包在 `with bind` 里；kb 的 `remember` 在图外、也不读 `skip_save()`。所以 kb 的落库判断靠 `except` 包住 `run_ask` + `remember` 前查 `cancelled()`。
- **断开要自己打取消标记**：`bus.cancel()` 全仓库没有调用方（`chatbus.py` 只定义、不调用，`iter_sse` 也不打标记）。`api/kb.py` 外包一层**异步生成器** `_sse_with_cancel(bus, stream_id, deadline)`（`iter_sse` 是 `async def`，`yield from` 是语法错误，要用 `async for`，见 D5）：`finally` 里 `bus.cancel(stream_id)`。`KbStreamer.cancelled()` 读 `bus.cancelled(stream_id)`，`remember` 之前再查一次 `cancelled()`。正常结束时 `remember` 发生在 SSE 读到 `done` 之前、`finally` 还没跑，不会误伤成功落库；断开若发生在模型已返回、`remember` 尚未调用的窗口里，这一眼能拦住。
- 取消时 `open_session` 已插过一行空会话，侧栏可能多一条空标题，前端显示「未命名对话」即可，不为它加删除接口。
- `result` 事件塞 kb 的 payload（`answer` + `hits[]` + `session_id`），复用 `run_ask` 返回的 hits 结构（含 `image_url`）。

**redis 未配时的行为**：`open_bus()` 返回 None（`config.REDIS_PASSWORD` 为空或连不上），端点回 503。服务器 `.env` 确认 `REDIS_HOST/REDIS_PORT/REDIS_PASSWORD`（项目已起 `db-redis`，密码 `Redis@2026`）。这是流式的硬前置。nginx `/kb/` 已带 `proxy_buffering off`，不必为流式再加 location。

### D3 · 前端 SSE（只保留一条回答气泡）

`App.vue` 的 `ask()` 从 `fetch('/kb/ask')` 一次取回，改为 `fetch('/kb/ask/stream')` + `readSse` 逐字渲染。对齐主站 `app.js` 的 `ask`（1419-1469 行）：

- 发送时先把用户问句放进线程；回答气泡**只有一个**，`text_delta` 往这条上追加。
- `result` 只补 `hits`、`session_id`（写入 localStorage **并且写回 `sessionId.value` 这个 ref**）；气泡还是空的再用 `result.answer` 填上。**不要再 `concat` 一轮**（否则同一轮画两遍）。
- **`result.session_id` 要同时写 `sessionId.value` 和 `localStorage`**：现有非流式成功分支两处都写（`App.vue:115-117`：`sessionId.value = body.session_id; localStorage.setItem(SESSION_KEY, body.session_id)`）。下一轮组表单读的是 ref（`if (sessionId.value) data.set('session_id', sessionId.value)`），`onMounted` 只在刷新时从 localStorage 回填 ref。只写 localStorage 的话，首轮后端已把 `sid` 放进 `result`，页面 ref 仍是空串，用户不刷新直接追问「那加班呢」时请求不带 `session_id`、`_session()` 再开新会话，指代和侧栏续传对不上。所以 `result` 到达且 `session_id` 非空时，ref 和 localStorage 一起写；气泡上只补 `hits`，正文已在流式气泡里。
- 然后 `loadHistory()` 刷新侧栏。
- `result` 以外至少处理 `error`。Redis 没配或并发满时端点回的是 JSON 503、不是 SSE：先看 `content-type`，不是 `text/event-stream` 就显示 `detail.message`/`error`，不要进 `readSse`（主站 `app.js:1437` 就是这么分的）。
- 图片提问时 `question` 为空，label 用 `file.name`（与现状一致），流式 label 无影响。
- **发送后清掉 `file`，但要在放进表单之后**：现在 `ask()` 只在成功后清 `question`、不清 `file`（`App.vue:105` 附近），而 `kb/workflow.py` 的 `_retrieve` 只要有图就走 `search_picture`。用户先传一张图、再问「那加班呢」，第二轮仍会带上这张图，命中块来自识图，指代验收会偏。清空顺序：校验租户通过 → 取出气泡文案（`file.name` 或「上传图片」）→ 把当前文件放进 `FormData` → **再**清 `file.value = null` 并把 `<input type="file">` 复位 → 发请求。若清空写在 `FormData.set('image', file)` 之前，这一轮的图会丢、图片提问验收失败。

### D4 · 流式回归要点

1. 文本提问：状态 → 逐字出答案 → 命中块在结束出现。
2. 图片提问：逐字出答案，命中图片块在结束出现。
3. 客户端中断（关标签页）两条路径都算取消，不 `remember`：① 生成过程中断开——`_sse_with_cancel` 的 `finally` 打 `bus.cancel()`，下一个 chunk 在 `stream_model` 里看到 `cancelled()` 并抛 `StreamCancelled`（`llm/agent.py:86-88、113-115`），走 `except` 分支；② 模型已返回、`remember` 还没调用——靠 `remember` 前再查一次 `cancelled()` 拦住。测试用 `MemoryBus.cancel(stream_id)`（假 `stream_model` 要停在 `cancelled()` 变真之前再抛 `StreamCancelled`，否则 `cancel` 晚于 `remember`，断言时好时坏），断言没有新的 message 行。
4. 流式落库：回答后侧栏出现该会话、打开旧会话能回放命中块、下一轮指代仍生效。
5. 非流式 `/kb/ask` 仍可用（评估/测试路径不回归，仍调 `invoke_model`）。

### D5 · `_sse_with_cancel` 的写法（异步生成器）

`iter_sse` 是 `async def`（`chatbus.py:182`），所以取消包装**不能**用 `yield from`（写在 `async def` 里是语法错误，写在普通生成器里也喂不进异步生成器）。用 `async for`：

```python
async def _sse_with_cancel(bus, stream_id, deadline):
    try:
        async for chunk in iter_sse(bus, stream_id, deadline):
            yield chunk
    finally:
        bus.cancel(stream_id)
```

`finally` 在读完或客户端断开之后执行。成功路径上 `remember` 在推出 `done` 之前，不会被这次 `cancel` 误伤。

---

## 八、验收（服务器上做）

开发机不起库、不跑迁移、不跑 pytest（沿用既有铁律）。本地改完 scp 到 `/opt/code/nutri_graph`，用 `/opt/envs/nutri/bin/python` 跑。

```bash
cd /opt/code/nutri_graph && /opt/envs/nutri/bin/python -m pytest tests/test_kb_ask.py -q
```

新增/扩展断言（`tests/test_kb_ask.py`）：

| 用例 | 断言 |
|---|---|
| `GET /kb/sessions?client_id=` 列表 | 该 client 名下的会话按 last_at 倒序；非法 client_id 返回 4xx |
| `GET /kb/recent?client_id=` | 返回该 client 最近 user 消息原话 |
| 多轮追问 | 第二问带 session_id，`run_ask` 的 history 非空（或 monkeypatch 校验 history 拼进 prompt） |
| 会话隔离 | client_a 的 session 列表不含 client_b 的会话（`kb:` 前缀隔离） |
| `/kb/ask/stream` 流式 | bus 用 MemoryBus 假实现；`stream_model` monkeypatch 成假流；断言 SSE 帧含 `text_delta` 和 `result`（hits 在内） |
| 流式成功后落库 | 流式提问返回后，`list_sessions` 出现该会话、`GET /kb/sessions/{id}` 能回放命中块（`remember` 被调用） |
| 取消不落库 | `MemoryBus.cancel(stream_id)`（或 `cancelled()` 返回 True）后，不新增 message 行 |
| 句末 flush | 假流输出的最后一段没有标点时，`KbStreamer.flush()` 仍把剩余原文推出（`text_delta` 完整，屏幕不截断） |
| 模型失败收尾 | 假 `stream_model` 抛模型池耗尽时，SSE 仍收到 `error` + `done`（前端不转圈），且信号量被释放 |

手工路径：

1. 打开 kb-web，选租户，连续追问 3+ 轮，确认指代理解、侧栏出现会话。
2. 新对话 → 再问，侧栏两条会话，点旧会话恢复，继续聊续传。
3. 超过 `MEMORY_RETAIN_TURNS` 轮后，确认更早内容仍可被理解（摘要生效），`session.summary` 落库。
4. 「最近问过」点一下再问一句。
5. 提问时答案逐字流出（不是一次整段出现），结束时命中块（含图片）出现。
6. 主站食疗问答侧栏（frontend）看不到 kb 会话（`kb:` 前缀隔离生效），食疗多轮不回归。

---

## 九、测试落点

| 文件 | 断言 |
|---|---|
| `tests/test_kb_ask.py` | 新增会话列表、最近问过、会话隔离、多轮 history 拼装、流式端点、流式落库、取消不落库、句末 flush、模型失败收尾九个用例（见第八节表）。复用现有 `kbclient01` 字符串，`ShortTermMemory` 用独立测试库 `nutri_graph_test` |

Milvus / DashScope 在测试里仍替换成假实现（沿用 v10 做法）。会话列表/最近问过是纯 PG 查询，不依赖 Milvus；流式端点用 `MemoryBus`（`chatbus` 里的测试实现）+ monkeypatch `stream_model`，不打真 Redis、不调真模型。取消不落库用 `MemoryBus.cancel(stream_id)` 触发，不靠真关浏览器；假 `stream_model` 要停在 `cancelled()` 变真之前再抛 `StreamCancelled`，否则 `cancel` 晚于 `remember`、断言时好时坏。
