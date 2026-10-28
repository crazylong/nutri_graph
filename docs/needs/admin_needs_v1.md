# 管理后台 v1 执行计划

> 状态：已按 `docs/review/review_admin_v1.md` 二轮修订，可实施。本文只定范围和接法，不含代码。
> 背景：现有 `/admin` 只有接口，没有页面。用户在问答或图谱探索里输入、库里没有的食物，写入 PostgreSQL `kg_ingredient_request`，管理员只能在 `/admin/stats` 看到各状态计数，看不到原话和明细。

## 1. 结论

后台做成独立的 Vue 应用，挂在 `/console/`。手机号登录复用现有验证码接口，登录后只有 `app_user.role = admin` 能进。第一期只做查看和审核，不做手工填写营养数据并写入主图。

`/admin/` 已经被 nginx 反代到接口。页面不能放在这个路径下，否则打开的是 JSON 而不是页面。问答页继续留在 `/`，不改成 Vue。

## 2. 第一期页面

| 页面 | 能做的事 | 数据从哪来 |
|---|---|---|
| 登录 | 手机号、验证码。同一 IP 发码超过免验证次数时，`/auth/sms` 返回 428、`captcha_required`，此时向 `/auth/captcha` 取 SVG，把 `captcha_id` 和答案随发码请求带回 | 已有 `/auth/sms`、`/auth/token`、`/auth/captcha`、`/auth/me` |
| 概览 | 用户数、未收录解决率、待审补录数 | 已有 `/admin/stats` |
| 未收录食物 | 看用户输入但库里没有的词：原话、归一名、次数、状态、驳回理由、来源、时间；可按状态筛选；可驳回；可手工登记一个食物名 | 新写列表接口。表是 `kg_ingredient_request` |
| 补录审核 | 看抓取结果，在明细里通过或驳回，通过后进主图 | 已有 `/admin/kg/bundles` 与 `.../{id}`。驳回按钮放在明细页，不在看不到证据的列表上直接驳回 |
| 候选关系 | 看待审边，通过或驳回。列表带上表里的 `sources`，驳回前能看到这条边从哪来 | 已有 `/admin/kg/candidates`。现有列表 SQL 没选 `sources`，这次补上该字段，不新开明细路径 |
| 疑似别名 | 只看「黑大豆 ↔ 黑豆」这类漏收别名，并显示当前 `status` | 新写只读列表。表是 `dict_alias_candidate`。这一期不加通过/驳回接口 |

用户侧两条登记路径保持不变，本计划不改它们：

- 问答里图谱对不上词，`graph/nodes.py` 的 `uncatalogued_node` 调用 `kg.request.register(..., source="chat")`。
- 图谱探索对照时，`POST /kg/compare` 对未收录食材调用 `register(..., source="compare")`。

疑似已有食材的别名写入 `dict_alias_candidate`，不建新食物。明显不是食物的词直接挡掉，不入库。

别名候选没有自己的审核按钮，但补录合并时 `kg/promote.py` 会把对得上的行改成 `approved`。只读列表展示库里的实时状态，不要把「这一期只读」理解成状态永远停在 `pending`。

## 3. 不做的事

- 手工填写营养成分、性味、搭配并写入图谱。主图只有「补录审核通过 → `kg.merge`」这一条写入口，页面上直接改图会绕开它。若以后要做，单独加「手工草稿 → 审核 → 合并」。
- 别名候选的通过/驳回按钮。状态可以被补录合并间接改掉，见第 2 节。
- 用户管理、在页面上把普通用户改成管理员。
- 改造问答页 `frontend/`。
- 在页面上执行 `scripts.run_fetch`。抓取进程仍走命令行。管理员登记一条新食物时，沿用 `register()` 往 `kg_fetch_task` 插一条排队记录，这和用户登记相同，不算在页面上启动抓取。

## 4. 登录与权限

复用现有手机号验证码，不新做一套账号。

- 登录成功后读 `/auth/me` 判断角色。只有这个接口返回 `role`。`/auth/principal` 对匿名也返回 `kind`，不能用来做管理员闸门。`role` 不是 `admin` 时停在提示页，不进菜单。`/admin/*` 本身已挂 `require_admin`，非管理员调接口是 403。
- 管理员没有自助开通入口。该手机号先登录一次建号，再在库里执行 `UPDATE app_user SET role = 'admin'`（按 `user_id` 更新）。`require_admin` 和 `/auth/me` 每次都从 `app_user` 读角色，不看访问令牌里的旧声明。改库后刷新页面、再请求一次 `/auth/me` 即可进入，不必重新发短信。
- 令牌单独存在 `ng.console.at` / `ng.console.rt`，不占用问答页的 `ng.at` / `ng.rt`，两边登录互不影响。
- 刷新、退出走已有 `/auth/refresh`、`/auth/logout`。

## 5. 技术接法

- 新目录 `admin-web/`：Vue 3、Vite、Vue Router、Element Plus。Vite `base` 为 `/console/`，路由用 `createWebHistory('/console/')`，两处一起改。
- 构建结果部署到 `/var/www/nutri_graph/console/`。`deploy/install.sh` 增加拷贝这一步。
- nginx 增加 `location /console/`，`try_files` 回落到 `/console/index.html`。`/admin/` 继续只反代接口。配置在 `deploy/nginx/nutri-graph-locations.conf`。
- `/console/` 下带内容哈希的 js/css 按文件名缓存即可。`index.html` 不长期缓存，避免改版后浏览器还拿着旧入口。这和问答页「入口不缓存、带版本的脚本可以缓存」是同一类约定。
- 与问答页同源，不另做跨域。

## 6. 要补的接口

全部挂在现有 `/admin` router 上，沿用 `require_admin`。驳回只改登记状态。手工登记调用 `register()`，并给它加一个跳过个人限额的开关。不写 Neo4j。

新列表统一用 `limit` + `offset`，返回 `count` 与 `items`。已有的补录列表、候选列表只有 `limit`，前端不要假设两边分页参数相同。

| 方法 | 路径 | 作用 |
|---|---|---|
| GET | `/admin/kg/requests` | 未收录列表。查询参数：状态、关键词、`limit`、`offset`。每条带 `source`、`reject_reason` |
| GET | `/admin/kg/requests/{id}` | 单条明细（原话、归一名、`hit_count`、提交者、状态、`source`、`reject_reason`、时间） |
| POST | `/admin/kg/requests/{id}/reject` | 驳回。`status` 改为 `rejected`，同时写入 `reject_reason`。理由为空也允许，字段落成空字符串 |
| POST | `/admin/kg/requests` | 管理员手工登记一个食物名。调用 `register(..., source="admin", who=当前管理员 user_id, skip_user_limit=True)` |
| GET | `/admin/kg/alias-candidates` | 疑似别名只读列表。查询参数：状态、`limit`、`offset`。返回行里的 `status` 以库里的当前值为准 |

管理员登记和用户登记共用 `register()`，但限额口径分开。现有签名是 `register(raw, who="anonymous", source="chat", near_misses=None)`，个人限额只按 `who` 计数，`source` 不参与判断。只传 `source="admin"` 仍会被默认每天 10 条挡住。

- 给 `register()` 增加 `skip_user_limit: bool = False`。为 `True` 时跳过 `FOOD_REQUEST_DAILY_LIMIT_PER_USER`。问答和图谱探索不传这个参数，个人限额与现在一致。
- 全站限额保留。`register()` 顶到 `FOOD_REQUEST_GLOBAL_DAILY_LIMIT` 时不写库，返回 `code="global_daily_limit"`，自己不抛 HTTP 错误。管理员接口要把这个结果转成明确的错误响应，不能当成登记成功。
- 第一次插入：`source=admin`，`status=queued`，并按现有逻辑插入 `kg_fetch_task`（同一 `food_key` 已有任务则不再插）。
- 食物名已经存在：仍是同一条，只增加 `hit_count`，刷新 `raw_mention` 和 `last_requested_at`。`source` 和 `status` 保持原值。原先是 `rejected` 的不会被这次登记重新打开，也不会因为状态不是 `queued` 而再排队。`source` 列没有 CHECK 约束，`admin` 可以写；注释里的 `chat | api` 过时，实施时改成实际在用的 `chat | compare | admin`。

补录与候选关系沿用已有接口，不改写路径：

- `GET /admin/kg/bundles`、`GET /admin/kg/bundles/{id}`、`POST .../approve`、`POST .../reject`
- `GET /admin/kg/candidates`、`POST .../approve`、`POST .../reject`。列表响应增加 `sources`
- `GET /admin/stats`

## 7. 执行顺序

1. 先改 `kg/request.py` 的 `register()`，增加 `skip_user_limit`（默认 `False`），只跳过个人日限额，全站限额仍在。再补第 6 节的 5 个接口；手工登记必须传 `skip_user_limit=True`。`tests/test_admin.py` 覆盖列表、越权（非管理员 403）、驳回时写入 `reject_reason`、手工登记（新行 `source=admin` 且有抓取任务、重复登记只增加 `hit_count`、个人日限额不拦截、全站日上限返回错误）。用户侧登记的既有测试保持通过。测试在服务器上跑（`/opt/code/nutri_graph`，`/opt/envs/nutri/bin/python`），不在开发机起库。
2. 搭 `admin-web`：登录闸门（含 428 图形验证码）、路由守卫、布局，以及概览、未收录、补录审核、候选关系、疑似别名五个页面。补录的驳回放在明细页。候选列表先展示 `sources` 再允许驳回。别名页只展示，不放通过/驳回按钮。
3. 改 nginx 与 `deploy/install.sh`，构建后部署到 `/console/`。
4. 用管理员手机号走一遍：登录、未收录列表、驳回理由可见、补录通过/驳回、非管理员被挡住。

## 8. 验收

- 浏览器打开 `/console/` 能看到手机号登录，不经过问答页。发码触发 428 时能完成图形验证码并继续登录。
- 改库把该手机号设成 `admin` 后，刷新页面即可进入菜单，不必重新收取短信。
- 管理员登录后能看到 `kg_ingredient_request` 里的原话、归一名、次数、状态、来源和驳回理由。驳回后状态为 `rejected`，列表和明细都能看到刚才写的 `reject_reason`。
- 手工登记一个新食物名后列表里出现，`source` 为 `admin`，并有对应的 `kg_fetch_task`。与已有记录重复提交时仍是同一条，`hit_count` 增加，`source` 和 `status` 不变。个人日限额不拦截这次登记；全站日上限到了则失败并给出错误。
- 待审 bundle 打开明细后可以通过或驳回；通过后的合并仍只走现有 `approve` 接口。候选列表能看到 `sources`。
- 疑似别名列表能看到 `pending` 和已被补录合并改成 `approved` 的行，页面上没有通过/驳回按钮。
- 普通用户登录后进不了菜单，直接调 `/admin/kg/requests` 返回 403。
- `/` 问答页和 `/admin/` 已有接口的行为与改前一致。候选列表多返回 `sources` 不算行为变化。
- 重新构建部署后，浏览器能加载新的 `/console/` 入口和带哈希的静态文件，不停留在旧包。
