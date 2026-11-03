# plan_v12 —— 后台用户、租户、知识库登录、企业客户

> 生成时间：2026-11-02
> 上游：[`docs/needs/needs_v12.md`](../needs/needs_v12.md) 第五节。5.3 三条都已按建议拍板。
> 与第四节 4.2 冲突时，以第五节为准。下面这些已经写进正文，不要再回 4.2 抄：
> 本期不做降级，不做 `SUPER_ADMIN_PHONE`；客户的令牌、刷新、同意记录不塞进指向 `app_user` 的外键；已存在的普通用户走后台登录返回 403，不升成管理员；知识库每次请求再校验租户；存量删 `role=user`，只把留下的后台账号挂 `default_renter`；租户名就是主键；用户页不改租户。
> 性质：只写怎么施工。食疗问答的检索和记忆算法、知识库的检索和审核流程不动。

---

## 一、范围

| Phase | 内容 | 大致人日 |
|---|---|---|
| A | 迁移：客户表、租户表、`username`；删存量食疗前台账号；回填租户 | 0.5 |
| B | `/auth/token` 分成后台、知识库、食疗前台三路；客户令牌另存 | 1.5 |
| C | 后台接口：用户、租户、客户列表 | 1.2 |
| D | 知识库改为登录态，每次请求校验租户 | 1.0 |
| E | 后台三个页面 | 1.0 |
| F | kb-web 登录，去掉匿名 `client_id` | 0.8 |
| **合计** | | **≈ 6.0 人日** |
| G | 服务器上迁移、pytest、页面点选 | 0.6 |

**顺序：A → B →（C 与 D 并行）→ E、F → G。** E 依赖 C 的接口。F 依赖 D 的接口。食疗前台页面不改，它已经在调不带 `console` 的 `/auth/token`。

---

## 二、本期不做

降级（管理员改回普通用户）、超管手机号配置、密码登录、角色表、租户改名、按调用者租户过滤后台文档、把存量食疗账号迁进 `customer`、合并旧的匿名知识库会话、账号锁定界面、注销、改手机号、客户的增删改。

一个号一旦是管理员，本期不能改回普通用户，也不能删。误设等以后做降级再处理。

---

## 三、要动的现有文件

| 位置 | 改什么 |
|---|---|
| `migrations/031_tenant_customer.sql`（新增） | 见 Phase A |
| `security/tokens.py` | 刷新的写入、旋转、吊销按表名工作。员工仍走 `refresh_token`，客户走 `customer_refresh`。员工 access 的 `typ` 仍是 `access`，客户是 `customer` |
| `api/auth.py` | `/auth/token` 三路分流。食疗前台建客户。后台只在新号上给管理员。知识库不建号 |
| `api/deps.py` | 员工鉴权继续只查 `app_user`。食疗前台的身份只接受客户令牌和匿名令牌 |
| `api/chat.py`、`api/history.py` | 身份依赖改到食疗这条。员工令牌 403 |
| `api/admin_users.py`、`api/admin_tenants.py`、`api/admin_customers.py`（新增） | 三个后台接口，路由整组挂 `require_admin` |
| `app.py` | 挂上这三个 router |
| `api/kb.py`、`kb/memory.py` | 问答、会话、图片改为登录用户 + 本次租户。会话属主 `kb:{user_id}:{tenant}` |
| `kb/store.py` | `/admin/kb/tenants` 改读 `tenant` 表 |
| `admin-web` | 用户、租户、客户三个页面和侧栏入口 |
| `kb-web/src/App.vue` | 登录门、租户输入框、带令牌请求。图片改 fetch |
| `tests/helpers.py` 及现有登录用例 | 见第八节 |

**不动**：`memory/short_term.py`、`memory/anonymous_merge.py`、`security/crypto.py`、`security/sms.py`、`frontend/assets/app.js`、`kb/search.py`、`kb/workflow.py` 的检索。`workflow.py` 里的 `image_url` 仍是 `/kb/images/{id}?tenant_id=`，鉴权放在接口上，不改这个字符串。

---

## 四、Phase A：迁移

新文件 `migrations/031_tenant_customer.sql`。`scripts/migrate.py` 按文件名只跑一次。下面的删除只在这一次执行里发生。

**表**

- `app_user` 加 `username TEXT NOT NULL DEFAULT ''`。展示名，登录仍用手机号。
- `tenant`：`name TEXT PRIMARY KEY`，`created_at`。`name` 就是 `kb_document.tenant_id`，也是登录框里填的租户名。检查约束与 `kb/common.py` 的 `TENANT_RE` 相同：`^[A-Za-z0-9_]{1,32}$`。
- `tenant_user`：`(tenant_name, user_id)` 主键。`tenant_name` 引用 `tenant.name`，`ON DELETE CASCADE`。`user_id` 引用 `app_user.user_id`，`ON DELETE CASCADE`。
- `customer`：`user_id UUID` 主键，手机号三态（`phone_enc` / `phone_hash UNIQUE` / `phone_mask`），`status` 默认 `active`，`created_at`，`last_login_at`。没有 `role`，没有租户。
- `customer_refresh`：列与 `refresh_token` 相同，`user_id` 引用 `customer`，`ON DELETE CASCADE`。
- `customer_consent`：列与 `user_consent` 相同，外键指向 `customer`。

不改 `refresh_token.user_id` 和 `user_consent.user_id` 上现有的外键。

**数据，按这个顺序**

1. 插入 `default_renter`。
2. 把 `kb_document.tenant_id` 里符合 `TENANT_RE` 的去重插入 `tenant`。不符合的留在文档行上，不进 `tenant` 表，本期不清洗。
3. 删存量食疗前台账号，只留后台账号。库里没有来源标记，按已拍板的角色区分：
   - 找出 `app_user.role = 'user'` 的 `user_id`。
   - 删这些 `user_id`（转成文本比较）名下的 `message`、`session`、`user_profile`、`memory_fact`。`message.session_id` 没有 `ON DELETE CASCADE`，先删消息再删会话。
   - `DELETE FROM app_user WHERE role = 'user'`。`refresh_token` 和 `user_consent` 靠现有外键级联。
   - 不删知识库文档，不删 `llm_call_log`。`role = 'admin'` 的食疗会话不删、不迁。
4. 把剩下的 `app_user` 全部挂上 `default_renter`（`ON CONFLICT DO NOTHING`）。按第 3 步之后，剩下的就是管理员。

从文档补进来的其他租户默认没有成员。知识库要进这些租户，得先在租户页把人挂进去。

---

## 五、Phase B：登录三路

`/auth/sms` 不变。验证码仍按手机号发，不区分员工和客户。

`TokenRequest` 增加两个字段，都有默认值，旧调用方不用改：

- `no_create: bool = False`。知识库登录传真。
- `tenant: str = ""`。知识库登录必填。

`console=true` 且 `no_create=true` 时走知识库这条，忽略升权。

### B1 · 后台登录（`console=true`，且不是知识库）

只写 `app_user`。

- 手机号还不存在：插入，`role=admin`，`username` 空串，挂 `default_renter`，签发员工令牌。这是 1.1，新手机号由此成为管理员。
- 已经是管理员：只更新 `last_login_at`。不改角色，不补租户。
- 已经是普通用户：403，`code=forbidden`，不签发令牌，不升管理员。
- `status` 不是 `active`：403，`code=account_locked`。
- 硬删之后同一手机号再登录：行已经没了，按新用户处理，重新成为管理员。

去掉现在这段：只要 `console=true` 就调用 `_grant_admin`。`api/auth.py` 的 `_grant_admin` 没有别的调用方就删掉。

### B2 · 知识库登录（`no_create=true`）

不插入任何表。

- 租户名为空，或不符合 `TENANT_RE`：422。
- 先校验验证码。验证码错仍是现在的 401 `invalid_code`。
- 验证码对了之后：`app_user` 里没有这个手机号，或这个人不在该租户的 `tenant_user` 里，都返回 401，`code=user_not_found`，`message=用户不存在`。两种失败用同一句，租户名合法但表里没有这个租户，也走这一句。
- `status` 不是 `active`：403 `account_locked`。这句和「用户不存在」分开，与现在的锁定文案一致。
- 通过后签发员工令牌。响应里带回本次租户名，供页面记住。租户不写进令牌。换租户不用重新登录。

### B3 · 食疗前台（两个标志都是假）

只写 `customer`，不写 `app_user`，不升管理员。手机号已在员工表里也照样建或登录客户行。同一个人两边各有一行。

- 没有客户行就插入，有则更新 `last_login_at` 和手机号三态。
- `status` 不是 `active`：403 `account_locked`。
- 请求里带了 `anon_token` 时，继续调用现有的 `merge_anon_into_user`，目标改成这个客户的 `user_id`。`session.user_id` 是文本，归并函数不用改。
- 请求里带了 `policy_version` 时写入 `customer_consent`，不写 `user_consent`。

食疗前台 `frontend/assets/app.js` 不改。它用 `kind === 'user'` 判断已登录，用 `role !== 'admin'` 走配额。因此客户身份返回给 `/auth/principal` 和聊天依赖时，`kind` 仍是 `user`，`role` 仍是 `user`，并带上 `phone_mask`。员工和客户的区别在令牌的 `typ` 和服务端查哪张表，不改前台这个判断。

### B4 · 令牌

员工 access 保持 `typ=access`。`get_current_user`、`require_admin`、`/auth/me` 只认这种，并且只查 `app_user`。

客户 access 使用 `typ=customer`。`decode_access` 继续拒绝它，所以客户令牌打后台接口是 401。

`security/tokens.py` 里刷新令牌的写入、旋转、吊销抽成按表名调用的内部函数，两张表各走各的。旋转时「先提交吊销，再抛重放异常」这个顺序保持，不要复制出第二份会回滚的写法。`/auth/refresh` 和 `/auth/logout` 按哈希先查 `refresh_token`，没有再查 `customer_refresh`。

`/auth/me` 和 `DELETE /auth/me` 仍只服务员工。客户令牌调用得到 401。本期不做客户注销。

### B5 · 食疗接口不认员工令牌

`api/chat.py`、`api/history.py`、`GET /auth/principal` 改用新的依赖：

- 无令牌：和现在一样，匿名流程或未登录。
- `typ=anon`：和现在一样。
- `typ=customer`：查 `customer`，返回 `kind=user`、`role=user`。
- `typ=access`：403，`code=staff_token`。不要把员工的 `user_id` 当成食疗用户，也不要当成没带令牌。

---

## 六、Phase C：后台接口

三个 router 都像 `api/admin_kb.py` 一样，整组挂 `require_admin`。列表分页与短信记录相同：`page` 从 1 开始，`page_size` 默认 20。响应只出 `phone_mask`，不出明文手机号。手机号筛选走 `phone_hash` 等值匹配，格式不合法返回 422。

### C1 · `GET/POST/PUT/DELETE /admin/users`

查询参数：`phone`、`username`（模糊）、`role`（`user` 或 `admin`）。不按租户裁剪。

返回：`user_id`、`username`、`phone_mask`、`role`、`created_at`、`last_login_at`。

- 新增：`phone`、`username`、`role`。`role` 只能是 `user` 或 `admin`。插入后挂 `default_renter`。手机号已存在返回 409 `phone_exists`，不改原行。
- 修改：只收 `username` 和 `role`。不收手机号，不收租户。当前是普通用户且请求把角色改成 `admin`：允许。当前已是 `admin` 且请求把角色改成 `user`：403，`code=forbidden`。只改用户名时角色不动。
- 删除：目标是管理员则 409，`code=admin_undeletable`。普通用户 `DELETE FROM app_user`。令牌、同意记录、`tenant_user` 级联。`kb:{user_id}:...` 的会话不删。

### C2 · `/admin/tenants`

- `GET /admin/tenants`：全部租户，带 `name`、`created_at`、成员数。
- `POST /admin/tenants`：body 是 `name`。过 `check_tenant`。重名 409。
- `DELETE /admin/tenants/{name}`：`default_renter` 返回 409。`kb_document.tenant_id` 等于这个名字时返回 409。其余删除，成员关系级联，员工账号保留。没有改名接口。
- `GET /admin/tenants/{name}/users`：成员的 `user_id`、`username`、`phone_mask`、`role`。
- `POST /admin/tenants/{name}/users`：body 是手机号。哈希后找 `app_user`，找不到 404。已是成员则 200，不报错。
- `DELETE /admin/tenants/{name}/users/{user_id}`：只删关联。人可以变成零个租户，之后知识库登录会得到「用户不存在」。

`kb/store.py` 的 `list_tenants()` 改成 `SELECT name FROM tenant ORDER BY 1`。`/admin/kb/tenants` 因此能看到还没有文档的租户。文档上那些不合法的旧 `tenant_id` 不再出现在这个下拉里，文档行还在。

### C3 · `GET /admin/customers`

只读。查询参数：`phone`、`created_from`、`created_to`（日期，含两端）。两个日期都空则不加时间条件，不要照短信记录默认成最近 7 天。

返回：`phone_mask`、`created_at`。没有新增、修改、删除。

---

## 七、Phase D：知识库登录态

`api/kb.py` 顶部那句「不登录」改掉。下列接口改为 `get_current_user`（员工令牌）：

`POST /kb/ask`、`POST /kb/ask/stream`、`GET /kb/sessions`、`GET /kb/recent`、`GET /kb/sessions/{session_id}`、`GET /kb/images/{chunk_id}`、`GET /kb/tenants`。

不再读取 `client_id`。不带员工令牌一律 401。

**每次请求的租户**

登录时的校验只保证当时能进门。问答、拉会话、取消息、取图片都再做一次：

1. `tenant_id` 为空或不符合 `TENANT_RE`：422。
2. `tenant_user` 里没有（当前用户，该租户）：403，`code=forbidden`。这里人已经登录，不用再回「用户不存在」。
3. 图片在归属通过之后，仍按现在的规则比对 chunk 的 `tenant_id`，对不上 404。

`GET /kb/tenants` 只返回这个人已经加入的租户，不返回全表。

**会话属主**

`kb/memory.py` 增加 `owner(user_id, tenant) -> str`，结果是 `kb:{user_id}:{tenant}`。直接交给 `ShortTermMemory`。不要调用 `check_client`，现有正则不允许冒号。

`list_sessions`、`recent_questions`、`messages`、`open_session` 的入参从 `client_id` 改为这个属主。同一 `session_id` 不能换租户用：属主不同，`owns` 会 404。页面切换租户时丢弃当前 `session_id`，开新对话。

旧的匿名 `kb:{client_id}` 会话留在库里，不迁移，新接口读不到。

---

## 八、Phase E：后台页面

沿用 `admin-web` 现有的 `el-table`、筛选条和 `api()`。侧栏在 `Layout.vue` 增加三项，`router.js` 增加路由：

| 路由 | 页面 | 行为 |
|---|---|---|
| `/users` | 用户 | 手机号、用户名、角色筛选。新增弹窗填手机号、用户名、角色。编辑只能改用户名；角色下拉在目标已是管理员时不能选普通用户。管理员那一行不放删除 |
| `/tenants` | 租户 | 新增名字、删除。`default_renter` 不放删除。点进一个租户后按手机号加入成员、移除成员 |
| `/customers` | 客户 | 手机号和创建日期范围。表格只有掩码和创建时间，没有操作列 |

`/auth/me` 仍要求 `role === admin`，`Layout.vue` 这段不用改。普通用户拿不到后台令牌，登录页展示接口返回的 `message`。

---

## 九、Phase F：kb-web

`App.vue` 在未登录时显示登录门，不显示问答区。

- 手机号、验证码，复用 `/auth/sms` 和图形验证码，与后台登录页同一套交互。
- 租户是输入框，初始值 `default_renter`，可改。不要在登录前请求全量租户。
- 登录调用 `/auth/token`，body 带 `no_create: true` 和 `tenant`。失败文案用接口的 `message`（「用户不存在」或锁定）。
- 令牌单独存放，键名用 `ng.kb.at` / `ng.kb.rt`，不和后台的 `ng.console.*`、食疗前台的键混用。
- 登录成功后请求 `GET /kb/tenants`，租户下拉只渲染这份列表。当前值是登录时填的那个。切换租户时清空 `sessionId` 再拉该租户的历史。
- `ask`、历史、最近问过都带 `Authorization`。表单继续传 `tenant_id`，不再传 `client_id`。删掉 `CLIENT_KEY` 和 `clientId()`。
- 命中块里的图片：`fetch(image_url)` 带同一个 `Authorization`，读成 blob 再给 `<img>`。`<img src>` 带不上令牌。

---

## 十、测试与验收

开发机不起库、不跑迁移、不跑 pytest。代码同步到服务器后：

```bash
cd /opt/code/nutri_graph && /opt/envs/nutri/bin/python -m scripts.migrate
cd /opt/code/nutri_graph && /opt/envs/nutri/bin/python -m pytest tests/test_auth.py tests/test_admin.py tests/test_anon_merge.py tests/test_kb_ask.py tests/test_profile.py -q
```

涉及这几张表的用例用独立库 `nutri_graph_test`（沿用现有 `PG_DSN` 的测试配置）。迁移里的删除跑在测试库时，会删掉测试库里当时 `role=user` 的行。这是预期行为，用例不要依赖迁移前的普通用户残留。

**助手函数**

- `tests/helpers.py` 的 `login()` 保持不带 `console`。它从此创建的是客户，食疗用例继续用它。
- 另加 `login_console()`，带 `console=true`，得到管理员员工令牌。
- 现在「先 `login()` 再 `UPDATE app_user SET role=admin`」的用例改成 `login_console()`。这类更新会落空，因为不带 `console` 的登录不再写 `app_user`。已知落点：`tests/test_admin.py`、`tests/test_kb_publish.py`、`tests/test_trace_detail.py`、`tests/test_request_triage.py`、`tests/test_sms_delivery.py`、`tests/test_admin_import.py`。

**要覆盖的断言**

| 用例 | 断言 |
|---|---|
| 后台新手机号 | `app_user.role=admin`，且在 `default_renter` 里。不写 `customer` |
| 已有普通用户带 `console=true` | 403，角色仍是 `user` |
| 已有管理员再登后台 | 角色不变，`last_login_at` 更新，租户关系不变 |
| 知识库登录，人在该租户 | 200，员工令牌 |
| 知识库登录，人不存在或不在该租户 | 都是 401，`message` 都是「用户不存在」 |
| 知识库登录后用另一个租户调用 `/kb/ask` | 403。用所属租户 200 |
| 食疗 `/auth/token` 不带标志 | 只写 `customer`。同一手机号可以同时有员工行。`/auth/me` 用这枚令牌是 401。`/chat` 可用，`/auth/principal` 的 `kind` 是 `user` |
| 员工令牌调用 `/chat` | 403 `staff_token` |
| 新增用户 | 挂上 `default_renter`。重复手机号 409，原角色不变 |
| 把普通用户改成管理员 | 200。把管理员改成普通用户 403。删除管理员 409。删除普通用户后行没了，知识库会话行还在 |
| 删除 `default_renter` | 409。删除名下有文档的租户 409。删除空租户后成员关系没了、人还在 |
| 客户列表 | 只能 GET。手机号精确、日期范围生效。响应无明文手机号 |
| 迁移 | 测试库里预先插入的 `role=user` 被删掉，其 `session` 被删掉；`role=admin` 还在并挂着 `default_renter`；该管理员名下的旧 `session` 还在 |

`tests/test_kb_ask.py` 里用 `client_id` 调问答的用例，改成先准备一个已加入租户的员工再带 Bearer。匿名会话用例删除，不保留「不登录也能问」。

`tests/test_anon_merge.py` 仍走不带 `console` 的登录。归并后的 `session.user_id` 应等于客户 `user_id`，不应去 `app_user` 里找这行。

**手工路径**

1. 后台用一个新手机号登录，侧栏能进用户、租户、客户。用户列表里这个号是管理员，在 `default_renter` 下。
2. 手工新增一个普通用户。他打开后台登录，看到失败，角色仍是普通用户。把他升为管理员后，后台登录可以进入，且不能再改回、不能删除。
3. 新建租户，把这个人加进去。知识库登录页填该租户名可以进；改填一个他不属于的名字，提示用户不存在。进去之后把请求里的租户改成另一个，问答被拒绝。切换下拉框后是新会话，历史不串。
4. 食疗前台用同一个手机号登录，客户列表出现掩码。这个前台令牌打不开后台，也打不开知识库。
5. 食疗前台的历史会话里看不到 `kb:` 开头的知识库会话。
