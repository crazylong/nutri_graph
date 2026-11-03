# plan_v12 评审（对照 needs_v12 第五节「需求复审」）

> 对照 [docs/needs/needs_v12.md](../needs/needs_v12.md)（以**第五节**为准，5.2 已拍板、5.3 已补意见）与 [docs/plan/plan_v12.md](../plan/plan_v12.md)。
> 未改代码。日期：2026-11-02。
> 这是对 plan_v12 的评审：判断「能不能照 plan 开工」，并把施工时必须额外核对的风险点列出来。§1 是结论，§2 是施工核对点，§3 是第五节结论的覆盖核对。

---

## 1. 结论

可以按 plan_v12 开工。plan 已经完整落实第五节的结论，没有照抄第四节 4.2 里被复审推翻的口径（降级、`SUPER_ADMIN_PHONE`、用户页改租户、存量账号迁 `customer` 这些都没出现）。

第五节五条已拍板 + 三条待拍板（已补意见）在 plan 里都有落点，且落点一致、无冲突。剩下的不是「改 plan」，而是施工时几个容易出错的地方要额外核对，见 §2。

---

## 2. 施工核对点（不是 plan 错误，是落地时最可能踩的坑）

### 2.1 匿名归并 `merge_anon_into_user` 与「分表」的潜在冲突

plan §三写「`memory/anonymous_merge.py` 不动」，§五 B3 又要求匿名归并目标改成 `customer` 的 `user_id`。这两句要落地，前提是 `merge_anon_into_user` 内部**没有**「按目标 `user_id` 去 `app_user` 查行校验存在」的逻辑。

- 若它只做 `session.user_id` 文本改写（plan 假设 `session.user_id` 是 TEXT，归并函数不用改），则成立。
- 若它内部有 `SELECT ... FROM app_user WHERE user_id = %s` 之类的校验，就得在不动该文件的前提下额外处理（比如目标行已存在于 `customer`，直接传 `user_id`）。

**施工时先读一遍 `memory/anonymous_merge.py` 确认它不依赖 `app_user` 主表**，再写 B3。这是 plan 里唯一「标着不动、但行为目标已换」的交叉点。

### 2.2 四条身份依赖的分工，别串

分表后身份解析从「一条 `_resolve`」变成「员工 / 客户 / 匿名」三条路径，落在这四个依赖上：

| 依赖 | 认什么令牌 | 查哪张表 |
|---|---|---|
| `get_current_user` / `require_admin` | `typ=access`（员工） | `app_user` |
| `/auth/me`、`DELETE /auth/me` | `typ=access` | `app_user` |
| 食疗 `get_principal`（chat/history/principal） | `typ=anon`、`typ=customer` | `customer` |
| 客户 access 打后台 | `decode_access` 拒绝 | — |

最容易出 bug 的是：**员工令牌调 `/chat` 要 403（`staff_token`），不能当成「没带令牌」走匿名，也不能当成食疗用户**。plan §五 B5 已写这条，但实现时 `get_principal` 的异常分支要单独区分 `typ=access` 和「无令牌」，否则员工令牌会被静默当成匿名用户。这条要写进测试断言（plan §十已有「员工令牌调用 /chat → 403 staff_token」，保留）。

### 2.3 刷新令牌「按表名抽函数」的重放语义

plan §五 B4 要求把 `refresh_token` 的写入/旋转/吊销抽成按表名调用的内部函数，并强调「旋转时先提交吊销、再抛重放异常」的顺序不能复制成第二份会回滚的写法。

这是正确且必要的（`customer_refresh` 与 `refresh_token` 必须共享同一套重放检测语义）。施工时注意：**抽函数是重构，不是复制**——抽完后员工令牌的刷新路径必须跑通现有 `test_auth.py` 的刷新/重放用例，证明重构没改行为。

### 2.4 迁移删除是破坏性，测试库会受影响

plan §四 Phase A 第 3 步删 `role=user` 的存量行 + 名下会话/画像/记忆。plan §十已提醒「测试库跑迁移会删掉当时 `role=user` 的行，用例不要依赖迁移前的普通用户残留」。

补充一点：**prod 库（`nutri_graph`）的迁移是一次性且不可逆的删除**。按记忆里「跑迁移前先备份」的铁律，prod 上执行 `scripts.migrate` 前先对 `app_user`/`session`/`message`/`user_profile`/`memory_fact` 做一份快照（或至少 `pg_dump`），防止「哪些行算前台账号」判断错了误删。

### 2.5 `kb-web` 图片改 fetch 的会话与缓存

plan §九把命中块图片从 `<img src>` 改成 `fetch(image_url)` 带 `Authorization` 读 blob。两点注意：

- `/kb/images/{chunk_id}` 现在还有 `tenant_id` 查询参数（plan §三说鉴权放接口上、不改 workflow 里的 `image_url` 字符串）。fetch 时要**同时带 `tenant_id`**，否则接口按归属比对可能 404。
- 改成 blob 后图片不再走浏览器缓存，流式回答里命中块多张图时会有几次额外请求，demo 可接受，但要留意别在每次重渲染时重复 fetch（用 `<img :src="blobUrl">` 且缓存 blob 一次）。

---

## 3. 第五节结论覆盖核对

| 第五节结论 | plan 落点 | 判定 |
|---|---|---|
| 5.2.1 本期不做降级、不做 `SUPER_ADMIN_PHONE`、用户页不能 admin→user | §二「本期不做」、§六 C1（admin→user 403） | 已落实 |
| 5.2.2 客户/员工两套业务，代码能共用就共用、共用不上另写 | §四 customer 表族、§五 B3/B4/B5 | 已落实 |
| 5.2.3 手工新增挂 `default_renter`，用户页不改租户、角色只能升 | §六 C1（新增挂默认租户、修改只收 username+role） | 已落实 |
| 5.2.4 租户名=文档 `tenant_id`，`default_renter` 禁删，无改名接口 | §四（`name` 主键）、§六 C2（DELETE 409） | 已落实 |
| 5.2.5 存量食疗账号删除、只留后台账号 | §四 Phase A 第 3 步（删 `role=user`） | 已落实 |
| 5.3.1 已存在普通用户后台登录 403 不升管理员 | §五 B1（「已经是普通用户：403」） | 已落实 |
| 5.3.2 知识库每次请求再校验租户归属 | §七「每次请求的租户」（403 `forbidden`） | 已落实 |
| 5.3.3 删 `role=user` + 名下会话/画像/记忆一起删，admin 保留挂默认租户 | §四 Phase A 第 3/4 步 | 已落实 |
| 5.4 维持口径（客户无租户、掩码、role 两值、管理员看全租户等） | §六 C3、§六 C1/C2、§七 `GET /kb/tenants` 只返回已加入租户 | 已落实 |
| 5.5 覆盖核对（1.5 移出本期等） | §二「本期不做」明确降级/超管移出 | 已落实 |
| 5.6 本期不展开 | §二 一致 | 已落实 |

---

## 4. 结论（复述）

plan_v12 与 needs_v12 第五节一致，可以开工。上面 §2 五个核对点是**施工注意事项**，不是 plan 缺陷，不需要改 plan。唯一建议在动 B3 之前先读一遍 `memory/anonymous_merge.py`，确认匿名归并目标从 `app_user` 换到 `customer` 不会踩到主表校验。
