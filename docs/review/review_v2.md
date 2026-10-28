# needs_v2 执行计划评审

> 对照 `needs_v2.md` 审查 `docs/plan_v2.md`。
> 对照的是当前仓库里的调用关系，不是计划文档的自述。未改代码。
> 日期：2026-10-28。

## 1. 结论

方向对，原文不能直接开工。

三条需求都该做，而且都该落在现有表上：日志走 `utils/logging_config.py`，历史对话用已有的 `session` / `message`，长期特征扩展 `user_profile`。计划里「扩展画像、不另起一套记忆」「登录接口里不预加载历史」「抽取前保留关键词预筛」这三处判断成立。

挡住执行的是三处和代码不符的设计：

1. 需求 1 被写成「已经接上，补文档即可」。`configure_logging()` 没有任何调用方，`app.py` 用的是 `logging.basicConfig`，轮转文件日志没有进进程。
2. 需求 2 多了一张 `search_history`，少了两件必做的事：校验 `session_id` 属于当前用户，以及「不传 session_id 就新开一会话」。现有 `get_session` 的行为正好相反，会把未结束的最近会话继续用下去。
3. 需求 3 的新字段有一半是标量。`parse_profile_from_answer` 只保留 `list`，年龄、性别、身高、体重在进库之前会被丢掉。`UserProfile.update` 又是整列覆盖，和计划里的「空值不覆盖」不是同一条写入路径。

计划正文里的「已确认：历史记录两者都要」没有写在 `needs_v2.md` 里。需求原文是一条：历史记录进 PostgreSQL，登录后加载，并能重启某次对话。会话列表已经覆盖这句话。单独的搜索词表是加出来的范围。

## 2. 分项判断

| 需求 | 计划是否对准 | 能否按步骤做 | 说明 |
|---|---|---|---|
| 1 日志 | 对准模块，看错现状 | 否 | 要改的是启动入口，不是写 `docs/logging.md`，也不是把脚本里的 `print` 换成 logger |
| 2 历史记录与重启对话 | 对准「列表 + 点进去接着聊」 | 改完语义再做 | `POST /chat` 已经收 `session_id`。缺的是归属校验、新开会话、登录后（含刷新）拉取列表 |
| 3 长期特征 | 扩展 `user_profile` 是对的 | 补上解析和写入规则再做 | 过敏已经在抽。新增的是年龄、性别、身高、体重、爱好、症状。抽取函数和覆盖语义还对不上这些类型 |

## 3. 必须先改的计划

### 3.1 需求 1：日志没有接上

计划 1.1 写「`configure_logging()` 已在 `app.py` 启动时调用，无需改」。全仓库只有 `utils/logging_config.py` 里定义了这个函数。`app.py` 启动时调用的是 `logging.basicConfig`，格式也不含 pid / thread，文件轮转不会发生。

业务模块里 `logging.getLogger("api.auth")` 这类名字已经在用。缺的是根 logger 的 handler。步骤应改成：

- 在 `app.py` 用 `configure_logging()` 替换 `logging.basicConfig`。`configure_logging` 会清掉根 logger 上的旧 handler，两边不要同时留。
- 请求路径继续用 `logging.getLogger("模块.子模块")`。`agent.run.*` 的分流过滤器已经在，不需要业务代码再选文件。
- `scripts/`、`kg/review.py`、`kg/constraints.py` 里的 `print` 是命令行输出，留给操作的人看。不要按计划 1.3 扫进 logger。

`docs/logging.md` 可以后补，不是这条需求的完成条件。完成条件是：进程起来之后，一条 `api.*` 日志出现在 `backend.log`，并按现有轮转配置切文件。

### 3.2 需求 2：会话生命周期和计划步骤互相矛盾

`ShortTermMemory.get_session` 的查询是：该用户 `ended_at IS NULL` 的会话里，取 `started_at` 最新的一条；没有才 `INSERT`。全仓库没有写入 `ended_at` 的语句。所以每个用户实际只有一条永远不结束的会话。

计划 2.6 写「不传 `session_id` 则开新会话」。按现在的函数，不传会回到那条未结束会话，新会话开不出来。计划 2.3 又写「不再依赖未结束 = 当前会话，改用最近活跃时间排序」。排序和「创建」是两件事。风险第 3 条点出了打架，步骤里没有改 `get_session`。

建议把步骤写成下面这组语义，并删掉「何时把 `ended_at` 写上」这种还没决定的分支：

- `POST /chat` 带了 `session_id`：只能续写当前用户自己的会话。
- 不带 `session_id`：总是 `INSERT` 新会话。不要再调用现在的 `get_session`。
- 列表按该会话最后一条 `message.created_at` 排序。没有消息时用 `started_at`。不必为了排序加 `last_active_at`，也不必靠 `ended_at`。
- 前端增加「新对话」。它清掉本地的 `ng.sid`，下一条提问不带 `session_id`。
- 标题用首条用户问题截断即可。`session.summary` 已有滚动摘要字段，`summarize_if_needed` 仍无调用方。不要为标题再打一次模型。

`POST /chat` 现在已经把 body 里的 `session_id` 放进图状态（`api/chat.py`）。前端 `ask()` 会带上 `localStorage` 的 `ng.sid`。计划 2.6 如果做成「给接口加一个参数」，会做成一次空改。真正要加的是归属检查：`session.user_id` 必须等于当前 `user_id`，对不上就 404，不要落到 `save_round`。现在没有这道检查。`load_context` 会把该会话最近几轮放进提示词，`save_round` 会往 `message` 里追加。别人的会话 id 一旦被收下，就会读到并写进别人的对话。重启历史对话把这个入口变成了正式功能，检查必须写进计划，不能留到实现时再想。

会话不存在时，`message.session_id` 有外键，`save_round` 会直接失败，然后被 `memory_write_node` 吞掉。未知 id 应同样 404，不要当成「记忆写失败但回答照常返回」。

登录后加载的触发点也不完整。计划写在 `/auth/token` 成功之后由前端拉 `/sessions`。`frontend/assets/app.js` 的 `bootstrap()` 在本地已有 access token 时会直接进入页面，不经过登录表单。只挂在登录成功上，刷新之后侧栏是空的。退出登录会清掉 `ng.sid`，历史只存在库里，页面上看不见。拉取应放在「已经是登录用户」的入口，登录成功和 `bootstrap` 走同一条。

列表接口要带 `user_id` 条件。`session` 表没有 `user_id` 索引，`message` 已有 `(session_id, created_at)`。计划给 `search_history` 建了索引，没有给真正要列出的 `session(user_id)` 建索引。

`GET /sessions/{id}/messages` 同样只返回当前用户的会话。消息按 `created_at` 正序。测试 3.9 写了分页，接口步骤没写页大小。个人会话列表先做固定上限（例如 50 条会话、单会话消息一次取完）即可，不必先做游标分页。测试步骤和接口步骤要对齐，否则验收项是空的。

注销已经按 `user_id` 删除 `session` 和 `message`（`DELETE /me`）。会话列表不需要额外的清理步骤。

### 3.3 需求 2：搜索词表是重复存储

`message` 里 `role = 'user'` 的 `content` 就是用户问过的话，已经在 `memory_write_node` 落库。需求要的「登录后加载，并可重启对话」由会话列表加上消息回放完成。

`search_history`（`query` / `query_norm` / `hit`）对不上这句需求：

- `query` 与用户消息重复。去重之后，两段以同一句话开头的对话会合成一条，重启时对不上会话。
- `hit` 是图谱是否命中，需求没要。
- 计划 2.2 要求建 `query_norm`，风险第 4 条又写「只回显就可以不加，联想以后再说」。步骤和风险互相否定。
- 新表不会自动进入匿名归并，也不会进入 `DELETE /me`。计划只提醒了画像新字段随行删除。行删除本来就会带走新列；真正会漏删的是这张新表。

若产品仍要一块「最近问法，点一下重新发送」（计划 2.11），用现有消息查即可：当前用户的会话下、`role = 'user'`、按时间倒序、限制条数。点击是再发一句新问，不是恢复旧会话。这和侧栏里「点进某次对话继续聊」是两个动作，计划里已经分开了，不需要第二张表来分开。

`009_history.sql` 保留给 `session.title` 和 `session(user_id)` 索引即可。迁移编号接在 `008_captcha.sql` 后面，这点没问题。

### 3.4 需求 3：标量字段会被解析函数丢掉

扩展 `user_profile` 而不是新建画像表，这个选择成立。现有列是 `allergies` / `conditions` / `diet_style` / `avoid_list`。过敏已经在抽。症状和 `conditions`（慢性病）不是同一类，失眠、生理期单独放 `symptoms` 是合理的。新列可空，旧行不受影响，这也对。

抽取链路有一个计划没写的过滤：

```169:173:graph/boundary.py
    try:
        obj = json.loads(m.group(0))
    except json.JSONDecodeError:
        return {}
    return {k: v for k, v in obj.items() if isinstance(v, list)}
```

年龄、性别、身高、体重不是 list。提示词即使按计划 3.3 加了这些键，返回值也会被丢掉，`UserProfile.update` 收不到。步骤里要改这个过滤：list 字段仍只收 list，标量另收 `int` / `float` / `str`，空串和 `null` 不进入更新字典。

`UserProfile.update` 对出现在字典里的列做 `EXCLUDED` 覆盖。模型某一轮只提到体重，若同时回了 `allergies: []`，过敏列会被写成空。计划的测试 3.8 要求「空值不覆盖旧值」，步骤 3.5 只写了把新字段放进 `allowed`。两处要合成一条写入规则：

- 本轮抽取：标量非空才覆盖；数组只做并集，空数组不写。
- 匿名归并（`memory/anonymous_merge.py`）：数组并集；标量哪边有值用哪边。两边都有值时用目标账号已有值，避免后登录的匿名画像盖掉账号上已经确认的身高体重。计划 3.6 的「非空优先」要写明是这一侧，不能理解成「库里的旧值永远赢」。否则用户后来说「我现在 60 公斤」，本轮更新也会被旧值挡住。

单位要写进抽取提示词。触发词含「斤」，列是 `weight_kg`。不换算的话，120 斤会存成 120 公斤。身高同理，只存厘米。性别收成固定的少数取值，不要把「女生吃什么」整句存进 `gender`。

爱好按需求要存。它不参与图谱过滤，也不必塞进每轮回答提示词。计划 3.7 只把人口学和症状放进提示词，这个范围是对的。

提示词本身还有一道门。`build_grounded_prompt` 要求只依据图谱事实、禁止常识推断。年龄和生理期要影响建议，就得在同一段提示词里写明：这些个人约束可以用来缩小建议，不能用来编造图谱里没有的关系。只把字段拼进 `allergy_note` 旁边，会被前面的禁令抵消。过敏今天能生效，是因为 `graph_query_node` 把过敏和忌口放进了 Cypher，不只是写在提示词里。年龄和症状没有对应的图过滤，提示词是它们唯一的使用点，这句授权必须写上。

画像加载点也要挪。`UserProfile.get` 只在 `graph_query_node` 里调用。直接走到 `vector_fallback` 或 `uncatalogued` 时，生成节点拿到的 `_user_profile` 是空的。人口学约束会在「没进图谱」的问法上消失。加载应放在生成之前的公共路径上，而不是绑在图查询上。

### 3.5 需求 3：触发词判断有一处是错的，词表也过宽

计划写：现有 `_PROFILE_CUES` 会漏掉「我今年35岁」和「我怀孕了」。第一句成立，`_PROFILE_CUES` 里没有年龄。第二句不成立，「怀孕」「孕妇」已经在词表里。怀孕现在的问题不是触发不到，而是抽出来之后只能放进过敏、慢病或忌口，三个字段都放不下。它应该进 `symptoms`（或同等的状态字段），而不是再加一遍触发词。

计划 3.4 建议加入的「男」「女」「喜欢」会把大量普通问法送去抽一次模型，例如「男人补锌」「女生经期吃什么」「我喜欢吃番茄」。后两句里的人也不一定是用户本人。提示词已有「仅当用户明确在说自己时才抽取」，触发层仍应避开这两个字。可用的触发是更长的说法：岁、身高、体重、公斤、斤、厘米、失眠、生理期、月经、爱好。保留「有触发才调用模型」，不要改成每轮都抽。这一条计划的权衡是对的，词表需要收窄。

`conditions` 继续只放慢性病。不要把生理期并进 `conditions`。

### 3.6 注销与归并要改的是语句，不是「新列会不会残留」

`DELETE FROM user_profile WHERE user_id = %s` 删的是整行。加上可空列之后，注销仍然会清掉年龄和症状。计划风险第 2 条把验收放在「新字段是否一并清除」上，这道检查会永远通过，看不出实现漏了什么。

要改并且要测的是列清单还写死的语句：

- `UserProfile.get` 的 `SELECT`
- `anonymous_merge.py` 里 `INSERT ... SELECT` 的列和 `ON CONFLICT` 的并集
- 若仍建 `search_history`，`DELETE /me` 和匿名归并都要加上这张表

`tests/conftest.py` 里有一份简化的 `session` / `user_profile` DDL，只在表不存在时执行，加列靠迁移即可，不必在计划里再维护一份平行建表。

## 4. 计划里可以保留的部分

- 需求 1 的日志模块选对了。分流规则、轮转、保留天数都在 `configure_logging` 里，不需要再设计一套。
- 需求 2 用现有 `session` / `message`，不新建对话存储。登录事务里不做历史查询，避免把发令牌的接口变重。消息回放和「继续向这条会话提问」分成两个接口，和现在 `POST /chat` 的职责吻合。
- 需求 3 扩展 `user_profile`，复用 `_defer_profile` 的后台线程，失败只记日志，不把 `/chat` 变成 500。症状与慢性病分列。数组在匿名归并时取并集，并要求幂等。这些和 `tests/test_anon_merge.py` 的现有口径一致。
- 迁移号 `009`、`010` 接在 `008_captcha.sql` 之后，顺序是对的。

## 5. 建议的修订顺序

1. 改计划 1.1：把 `app.py` 的 `logging.basicConfig` 换成 `configure_logging()`。删掉「替换全部 print」和「先写 logging 文档」作为完成条件。
2. 改计划 2.6 和 2.3：不传 `session_id` 一定插入新会话；传入则校验归属；列表按最后一条消息时间排序；前端在已登录入口拉取，并提供新对话。去掉 `search_history`。`009` 只加标题和 `session(user_id)` 索引。最近问法从 `message` 查。
3. 改计划 3.3–3.6：解析函数接受标量；本轮更新空值不覆盖、数组并集；归并时标量保留目标账号的非空值；提示词授权使用年龄和症状，并写明斤→千克。触发词去掉「男 / 女 / 喜欢」，补上年龄，怀孕归入症状。画像在生成前加载，不绑在 `graph_query`。
4. 测试只保留计划里已经点名、且和上面语义一致的几条：新会话与续写旧会话、越权 `session_id` 被拒绝、标量空值不覆盖、数组并集幂等、注销后画像行不存在。分页、`query_norm`、图谱命中标记先不测。
