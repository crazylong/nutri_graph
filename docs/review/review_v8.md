# plan_v8 评审（对照 needs_v8）· 第七轮

> 一 12:35（v8.0）→ 二 16:43（v8.1）→ 三 17:12（v8.2）→ 四 20:35（v8.3）→ 五 20:43（v8.4）→ 六 20:50（v8.5）。计划 20:56 改为 **v8.6**。
> 这轮把 v8.6 新写的 `note` 那段拿到线上跑了一遍（4 组用例），再核施工清单和几个边界。
> **未改代码。** 施工仍以 [docs/plan/plan_v8.md](../plan/plan_v8.md) 为准。

## 1. 结论

**设计层面我认为可以定稿了。** v8.6 补的 `note`（弱证据 / 单源免责前缀）不是照抄我的话写上去的，它把「有 `note` 时政治类会吞掉 `note`」「有 `note` 时 `MEDICAL_FALLBACK` 根本不会出现」这两条也从现有代码里读出来了 —— 这两条我实测验证过，**全对**（§3）。

剩下的四条都不是设计问题，是「施工清单没列全」和「一个边界会抛异常」：

1. 管理端页面加完要 `npm run build`，再把 `admin-web/dist` 同步到服务器的 `/var/www/nutri_graph/console/`。§七 的七步里没有这一步，照着做完，线上菜单是看不到的。
2. `.env.example` 没跟着加四个新变量（现状连 `SMS_PROVIDER` 都没有）。
3. 回执接口的 `compare_digest` 收到非 ASCII 的 `token` 会抛 `TypeError`（变成 500）。
4. Redis 键的 `EXPIRE 600` 和「按候选数算的 SSE 上限」是两个独立数字，候选一多，键会先于流过期。

前三条补进文档就能开工。

## 2. 第六轮问题落实情况

| 第六轮提的 | v8.6 的处理 | 判定 |
|---|---|---|
| `note` 整条被漏掉 | §4.6 新增整段：位置（`status` 之后、正文之前）、不进 `rewrite_answer`、比较用的已推文字只记模型正文、全文规则分三种收法、`answer_replace` 带 `note` | ✅ 实测一致，见 §3 |
| 全模型不可用 vs 空正文 | §4.3 新增一条：同步返回 200 + 固定话术并落库，流式同样推 `text_delta` + `result` + `done`，不发 `error`；两处同一段文案 | ✅ |
| 句间换行被吃掉 | §4.6：判等用 `rstrip` 副本，推送从未裁剪的那份切，已推文字也存未裁剪版 | ✅ |
| `answer_replace("")` 清不掉节点 | §4.6 分支内 + §4.7：「空串是撤回，不是没有内容所以跳过」 | ✅ |
| 全删时页面长时间空白 | §4.6：命中疗效兜底且页面无正文时补一条 `status`「正在核对表述」，同一轮只发一次 | ✅ |

## 3. 实测：v8.6 关于 `note` 的四条判断都对

在服务器 venv 上直接调 `rewrite_answer`（纯函数，不连库），输入是真实的两段免责文案：

| 输入 | 结果 | 与 v8.6 的说法 |
|---|---|---|
| `note + 「黑豆能治好糖尿病。」` | `OTHER`，返回值以 `note` 开头（疗效句被删，`note` 留下） | ✅ |
| `note + 「黑豆每100g含蛋白质36g。」` | `OTHER`，返回 `note + 正文` | ✅ |
| `note + 「这个能治糖尿病。还能降血压。」`（全删） | **`OTHER`**，返回值就是 `note` 本身，**不是** `MEDICAL_FALLBACK` | ✅ §4.6「拼在一起扫描时这句免责会被留下，兜底话术不会出现」 |
| `note + 「台独也能入菜。」` | `POLITICAL`，返回值是政治类固定话术，`note` 没了 | ✅ §4.6「政治类只有 `POLITICAL_ANSWER`，`note` 去掉」 |

所以 v8.6 定的那套「全文 = `note + 改写后正文`，正文被删光时只留 `note`」和今天的同步行为是等价的，没有引入新的口径差异。这一段可以照做。

## 4. 施工清单漏的两步

### 4.1 管理端页面要 build + 部署 dist

`admin-web` 是 Vue + Vite，线上跑的是编译产物。服务器上的实际布局（刚查的）：

```
/var/www/nutri_graph/
├── index.html  app.js  assets/  import.html  template/     ← 前台
└── console/
    ├── index.html                                          ← 后台（nginx /console/ 指到这里）
    └── assets/
```

§3.5 只写了「菜单加在 `Layout.vue`、路由加在 `router.js`、页面 `SmsLogs.vue`」，§七 的第 7 步是「前台改读 SSE」。**没有一步是「后台 `npm run build` 并同步 `dist` 到 `/var/www/nutri_graph/console/`」**。照这份文档做完，代码是对的、线上看不到菜单 —— 这类「本地好好的」最难排查。

建议在 §七 末尾加一步，并顺手记下：`console/index.html` 已经是 `no-store`（nginx 里单独配了），后台的静态资源是 vite 打过 hash 的，不需要像前台那样手改 `?v=`；但 `admin-web/dist` 与服务器上的 `console/` 必须整目录覆盖，只传 `index.html` 会留下旧 chunk。

### 4.2 `.env.example` 没有同步

`.env.example` 现在 134 个变量位，`SMS_PROVIDER` / `ALIBABA_CLOUD_*` 一个都没有 —— 这是 needs 13 就欠下的账，v8 又加了 4 个（`SMS_REPORT_TOKEN`、`REDIS_HOST`、`REDIS_PORT`、`REDIS_PASSWORD`）。§七 第 5 步只写「`config.py` 加」，建议同时补 `.env.example`，否则下次换机器部署，`REDIS_PASSWORD` 一漏就是流式 503，而新人根本不知道有这项。

## 5. 两个边界

### 5.1 `compare_digest` 遇到非 ASCII 会抛 `TypeError`

§3.4 写「与请求参数常数时间比较」。`hmac.compare_digest` 对 **str** 入参要求两边都是 ASCII，否则抛 `TypeError: comparing strings with non-ASCII characters is not supported`（不是返回 False）。所以 `?token=中文` 会让这个接口 500，而不是 404 —— 阿里云那边会当成处理失败重推三次。

改法一行：两边先 `.encode()` 成 bytes 再比（bytes 版没有这个限制），或先判 `token.isascii()`。§8 建议加一条「非 ASCII token 返回 404 而不是 500」。

### 5.2 `EXPIRE 600` 与「按候选数算的上限」是两个独立数字

§4.4 把 SSE 总时长改成 `(候选数 + 兜底) × LLM_TIMEOUT + 120`（默认 4 候选 + 兜底 = 570 秒），而列表键的 TTL 仍是固定 `600`。现在刚好差 30 秒，够用；但这两件事是耦合的：以后池里再加一个模型、或把 `LLM_TIMEOUT` 调到 120，上限就会越过 600，键会在流还没结束时就自己过期，表现是「说好的最长等待，提前变成 `error`」。

建议把 TTL 也写成同一个公式（上限 + 60），并且只在 `RPUSH` 之后刷新 —— 现在已经是「每次 `RPUSH` 后 `EXPIRE`」，所以只要把数字换成公式就行。

## 6. 本轮复验

| 说法 | 复验 | 判定 |
|---|---|---|
| `note` 的两种取值与拼接位置 | `graph/nodes.py:511-513`、`:591-592` | ✅ |
| 政治类会吞掉 `note` | 实测 `POLITICAL` | ✅ |
| 有 `note` 时不会出现 `MEDICAL_FALLBACK` | 实测全删场景返回 `note` 本身 | ✅ |
| 后台部署路径 | 服务器 `/var/www/nutri_graph/console/{index.html,assets}` | ✅ §4.1 的遗漏成立 |
| `.env.example` 现状 | 134 个变量位，无 `SMS_PROVIDER` | ✅ §4.2 |
| `compare_digest` 非 ASCII 行为 | Python 文档语义（str 仅支持 ASCII） | ✅ §5.1 |
| 候选数与上限 | `/llm/pool`：队列 3 + 兜底 1，`LLM_TIMEOUT=90` → 上限约 480～570 秒 | ✅ §5.2 |

## 7. 修订顺序

1. §七 末尾加「后台 `npm run build` + 整目录同步 `dist` 到 `/var/www/nutri_graph/console/`」。
2. §七 第 5 步补 `.env.example`。
3. §3.4 的 token 比较改成 bytes 比较（或先判 `isascii`），§8 加非 ASCII 用例。
4. §4.4 的 `EXPIRE` 改成与上限同一公式。

## 8. 可以直接照做的

前六轮的结论全部保持有效，这一版没有回退。特别是：

- §三 短信全线（token 未配置 503、`biz_id` 双层兜底、单行更新、上海时区、`sms_size`、422、nginx 四行、404 warning）。
- §4.3 的 `_Plan` 拆参、`stream_usage` 取最后一个非空、取消不记账、零正文四级兜底、全模型不可用走固定话术。
- §4.4 的 Redis 事实与七条约束（谁最后谁删、`error`+`done` 尽力推、`RPUSH` 失败不拖垮落库、contextvar 不进 `RAGState`）。
- §4.5 事件表、网页参考拼接顺序、`result` 统一组装、`session_id` 回填。
- §4.6 的 `note` 处理、按句判定顺序、`replace` 后同步 `pushed`、空串撤回（这一整段实测过，可以照做）。
- §4.7 前端卡片壳、`api()` 复用、`?v=` 换新。
