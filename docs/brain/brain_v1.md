# nutri_graph 生产化差距头脑风暴 · v1

> 生成时间：2026-10-31 · 基线：commit `6116bd0 (v8)`，含 needs_v8 / plan_v8 / review_v8 三轮结论
> 方法：读代码 + 读现有 design/review 文档，逐项核对「生产环境里会不会出事」，不给泛泛而谈的建议。
> 结论按优先级分三档：**P0 = 上线就会有人因此流失或被投诉**，**P1 = DAU 上千之前必须补**，**P2 = 差异化与长期护城河**。
> 每条给四件事：**现状（带出处）→ 风险 → 建议动作 → 验收口径**（怎么算做完）。

---

## 0. 一句话结论

**这不是「缺功能」，是「缺工程化」。** 算法侧的设计（证据分层、无据弃答、幂等写入、批量回滚、模型池熔断）已经明显超出 demo 水平，真正卡住上线的全是工程与合规的地基活：**单进程单点、无备份、无成本计量、无告警、无 CI、明文 HTTP、还是手工 scp 部署**。按 §7 的 M1 里程碑，大约两周的工程活能把「能演示」变成「敢挂到应用商店」。

---

## 1. 先认家底：已经很扎实的，别再动

避免重复投资，先把已经过关的列出来（这些都是简历上说得出手的点）：

| 已有能力 | 出处 | 状态 |
|---|---|---|
| 证据分层 `type × level` + 意图级过滤 | `config.EVIDENCE_POLICY_MAP` | ✅ |
| 结构上区分民间说法 vs 医学禁忌（两类边） | README「核心设计」 | ✅ |
| 无据弃答 + 降级纯向量 + `graph_hit` 标记 | `graph/nodes.py`、`api/chat.py` | ✅ |
| 幂等写入 + `batch_id` 整批回滚 | `kg/merge.py`、`python -m kg.merge --rollback` | ✅ |
| 单源数据 `provisional` 降级引用（不是藏起来） | `config.INCLUDE_PROVISIONAL` / `FOOD_TRUSTED_MIN_SOURCES` | ✅ |
| 手机号三态存储 + 四维频控 + 全局熔断 | `security/ratelimit.py`、`001_auth.sql` | ✅ |
| refresh token 旋转 + 重放整族吊销 | `security/tokens.py` | ✅ |
| 模型池自动切换 / 冷却 / 兜底 | `llm/pool.py`、`llm/middleware.py` | ✅ |
| 敏感词守卫 + 输出扫描 + 免责声明 | `kg/sensitive.py`、`config.SENSITIVE_*` | ✅ |
| 红旗症状硬拦截 | `config.RED_FLAG_SYMPTOMS` | ✅ |
| 审计追踪 + Ragas 分层评估 | `observability/tracing.py`、`eval/` | ✅ |
| 管理后台（Vue）覆盖候选/补录/登记/别名/短信/导入 | `admin-web/src/views/*`（14 个页面） | ✅ |

**这份家底意味着：内容质量的方向是对的，把精力放在「让它稳定地、可复现地、便宜地跑起来」。**

---

## 2. 差距总表

| 编号 | 标题 | 档 | 一句话 |
|---|---|---|---|
| OPS-01 | 全站单点 + 单 worker | P0 | 一个进程挂了就全站不可用 |
| OPS-02 | 没有任何备份/恢复脚本 | P0 | 图是核心资产，删了只能靠 `--repair-nodes` 补，孤儿数据救不回来 |
| OPS-03 | 手工 scp 部署、无 CI | P0 | 改一行代码要跨 5 步，且没有回滚能力 |
| SEC-01 | 明文 HTTP、IP 直访 | P0 | 验证码与回答全线可被抓包，且浏览器标「不安全」 |
| SEC-02 | 弱口令与默认凭据 | P0 | compose 里 Neo4j/PG/MinIO/Langfuse 全是默认值 |
| SEC-03 | consent 记了不用 | P0 | 注册写 `user_consent`，但画像抽取默认 True，个保法口径对不上 |
| COST-01 | 计量口径不全 | P0 | 有模型维度 token 累计，但没有用户/时间/费用维度 |
| COST-02 | 无用户级配额 | P0 | nginx 只按 IP 限，登录后一个账号能刷穿额度和账单 |
| QLT-01 | 评估不在流水线上 | P0 | 75 条数据集、faithfulness 0.697，改 prompt 无回归门禁 |
| QLT-02 | 无用户反馈闭环 | P1 | 答错了没人告诉我，0.697 就是天花板了 |
| PERF-01 | 并发天花板 ≈ 10 | P1 | sync 端点 + `--limit-concurrency 10` |
| PERF-02 | 每条 SSE 一个常驻线程 | P1 | 流式改造（needs_v8 二）上线后会放大成线程泄漏 |
| PERF-03 | 全链路零缓存 | P1 | 同一句「番茄鸡蛋」每天走几百次完整图查询 + 一次 LLM |
| OBS-01 | 只有 tracing，没有 metrics/告警 | P0 | Langfuse 是「事后查案」，不是「事前报警」 |
| OBS-02 | 无请求层标识 | P1 | 日志有 pid/thread，但拼不出一次请求的链路 |
| KGD-01 | Neo4j 测试与生产共图 | P0 | 已炸过一次（删 120 个食材），护栏是后加的 |
| KGD-02 | 来源无快照、无复检 | P1 | `locator` 只存 URL，链接失效即断证 |
| KGD-03 | 补录闭环靠人点 | P1 | 无定时任务，`run_fetch` 不点就不跑 |
| PRD-01 | 缺 C 端反馈与产品化外壳 | P1/P2 | 无小程序/App、无收藏、无方案生成 |

---

## 3. P0：上线前必须止血

### OPS-01 全站单点 + 单 worker

- **现状**：`deploy/systemd/nutri-graph.service` 里写死 `--workers 1 --limit-concurrency 10`，注释说明原因是「别名缓存、模型池状态都在进程内」。一台 ECS 上跑着 Neo4j / Milvus / PG / Redis / Langfuse，外加一个 API 进程。
- **风险**：进程崩、OOM、机器重启 → 全站不可用；`--workers 1` 意味着不能靠多进程扩容，CPU 只能吃满一个核；所有中间件在同一台机器上，一次 OOM 可能连锁带走 PG。
- **建议动作**（分两步，别一次到位）：
  1. **先把状态外置**：`data/llm_pool_state.json` 与别名缓存迁到 Redis（已在 `config.REDIS_*`），进程变无状态 → 可以 `--workers N`。注意 `llm/pool.py` 现在是文件 + 内存锁，改 Redis 前要处理多进程竞争（用 `SET NX` 或 Lua）。
  2. **再谈多副本**：先做到「两台应用进程 + nginx 负载均衡 + 共享 Redis」，中间件仍共用。真要多副本还得让 PG 连接池与 `UVICORN_LIMIT_CONCURRENCY` 联动（写 replica 前先看 `db/engine.py` 顶部三条坑）。
- **验收**：杀掉一个 worker 进程，服务仍可用，且模型池状态不丢；`uvicorn --workers 2` 下 `/llm/pool` 两个进程看到同一份状态。

### OPS-02 没有任何备份/恢复脚本

- **现状**：`scripts/` 18 个脚本覆盖迁移、导入、抓取、补录、校验与修复，但**没有一个做备份/恢复**；服务器上只有 `/opt/db/{postgres,neo4j}/backup` 空目录。历史教训已经证明风险真实：一次测试误删 120 个食材，靠 `scripts/backfill_nutrients.py --repair-nodes` 才恢复。
- **风险**：图是核心资产且**无法从零重建**（每条边的「谁、什么时候、凭什么加进来」在 `kg_candidate` 审计表与 PG/Neo4j 两处，不同源）；USDA 抓取一轮要几十分钟外加 API 额度。
- **建议动作**：
  1. `scripts/backup.sh`：PG 用 `pg_dump -Fc`（`nutri_graph` 14 张表）+ Neo4j 社区版用 `neo4j-admin database dump`（需停库或离线）→ 落 `/opt/db/backup/YYYYMMDD/`。Neo4j 社区版不支持热备，可接受「每天凌晨停 30 秒 dump」。
  2. `scripts/restore.sh`：写清楚恢复顺序（PG 恢复 → 重启 Neo4j → restore → `python -m kg.constraints` 重建成约束），并且**必须在测试机上真跑一遍**。没验证过的备份不是备份。
  3. 备份送出本机（OSS/cos Buckets），至少留 7 天。
- **验收**：在测试机执行一次完整 restore，图节点/边数量与备份前一致；`git blame` 能查到 backup.sh 上次修改日期。

### OPS-03 手工 scp 部署、无 CI/CD

- **现状**：无 `.github/`、无 Dockerfile；部署是「本地改 → scp 到 `/opt/code/nutri_graph` → `systemctl restart` → 前端还要手工 bump `index.html` 的 `?v=`」；`admin-web` 要 `npm run build` 再整目录同步 dist（review_v8 §4.1 已经踩过一次）。
- **风险**：每次发布都是一次人为事故窗口；前端 `/assets/` 强缓存 7 天，忘了 bump 就是「改了没生效」；出问题无法快速回滚（只能靠 scp 一份旧文件回去）。
- **建议动作**（性价比从高到低）：
  1. **一个 `deploy/release.sh`**：拉代码 → `pip install -r` → 跑迁移 → 跑冒烟 → `systemctl restart` → 健康检查结果断言。先把 5 步手工变成 1 步。
  2. **GitHub Actions 最小 CI**：`python -m pytest`（远程机或 self-hosted runner）+ `node --check frontend/assets/app.js` + `admin-web` 构建 + 产物打包。PR 上红绿灯。
  3. **再考虑容器化**：应用本身没什么系统依赖（就是 Python + 一堆 SDK），Dockerfile 的收益主要在「环境可复现」，而现在已经有一份能跑的 systemd 部署；**先把 1、2 做掉**，容器化优先级放低。
- **验收**：主线任一 push 都能在 CI 页面看到单测结果；rollback 操作 ≤ 1 条命令，且 ≤ 2 分钟。

### SEC-01 明文 HTTP + IP 直访

- **现状**：nginx 只挂在那台机 listen 80 的默认站点上（`deploy/nginx/*.conf` 是 include 片段），没有 server_name、没有 443 块；用户实际是 `http://8.134.253.167` 访问。
- **风险**：
  - 验证码、Bearer token、回答正文全线明文；公共 WiFi 可直接抓到用户的健康问句（「我孕 6 周能不能吃 X」）——这在《个保法》下是敏感个人信息泄露路径。
  - 浏览器地址栏「不安全」对健康类 App 是直接的信任崩塌。
  - 也没有 HSTS、没有 Secure Cookie 概念（虽然目前是 Bearer token，但迟早要加 Cookie）。
- **建议动作**：买域名 → ICP 备案（合规手册 §三已经在流程里）→ Let's Encrypt 免费证书 → nginx 443 全站 HTTPS + 80 强制跳转 + HSTS。**这一步与备案是同一条依赖链，建议和备案一起做，不要分两次。**
- **验收**：`curl -I http://域名` 返回 301；Qualys SSL Labs ≥ A；Nginx 配置里没有 `ssl_protocols SSLv3`。

### SEC-02 弱口令与默认凭据

- **现状**：`docker-compose.yml` 里 Neo4j 是 `neo4j/nutrigraph-dev`、PG 是 `nutri/nutri`、MinIO `minioadmin/minioadmin`、ClickHouse `clickhouse/clickhouse`、Langfuse `NEXTAUTH_SECRET=dev-secret-change-me...` / `SALT=dev-salt` / `ENCRYPTION_KEY=dev-encryption-key-000...`。项目记忆里也写着「`nutri/nutri` 是弱口令」。
- **风险**：这些是自动化扫描最先扫出来的东西；Langfuse 里存着问句原文，一旦被攻破就是数据泄露；MinIO / etcd 无鉴权更是常见勒索入口。
- **建议动作**：
  1. 所有中间件凭据随机生成，全部挪到 compose 的 `.env`；
  2. Langfuse 的 `NEXTAUTH_SECRET` / `SALT` / `ENCRYPTION_KEY` **必须换**（这三个在 compose 里是硬编码的 dev 值，最容易被忽略）；
  3. 中间件端口不要映射出宿主机公网网卡（5432 / 6379 / 7474 / 19530 只绑 127.0.0.1），需要远程调试走 SSH 隧道——项目记忆里为家庭宽带 IP 开的那条 pg_hba 规则，建议改成「调试时开、日常关」。
- **验收**：`grep -r "dev-" docker-compose.yml` 无结果；`ss -lntp` 里看不到任何存储端口绑在 0.0.0.0。

### SEC-03 consent 记了不用（合规口径对不上）

- **现状**：注册时会写一条 `user_consent(user_id, policy_version, ip)`（`api/auth.py:152`），但**之后再也没被读过**（全仓 grep 只剩注销时的 DELETE）。而 `api/chat.py:76-79`：

  ```python
  "consent_profile": True if raw_consent is None else bool(raw_consent),
  ```

  即：请求里不带 `consent` 字段 → 默认 True → 照样抽取长期画像。
- **风险**：合规手册 §四明确要求「敏感个人信息单独同意 + 必要性说明」，语义上应该是「不弹窗 = 不同意」，现在实现是「不传 = 同意」。这是上架审核和个人信息投诉里最容易被挑的一条，而且是 **15 行代码就能堵上的口子**。
- **建议动作**：
  1. 默认改成 `False`：不显式传 `consent=True` 就不抽画像、不写 `memory_fact`；
  2. 同意记录在 `user_consent` 里加一列 `sensitive_consent_at`（区别于注册时的政策版本同意），前端单独弹窗（文案合规手册附录 B 已有，直接抄）；
  3. 用户改口拒绝时，提供「清除已抽取画像」入口（复用注销时的 purge 逻辑）。
- **验收**：不传 consent 请求 `/chat`，事后查 `memory_fact` 表无新增；拒绝后调用清除接口，`user_profile`/`memory_fact` 归零。

> 更正记录：初稿曾写「无 token 计量（grep usage 零命中）」——**那句是错的**（当时的检索失效）。实际上
> `llm/middleware.py:160` / `llm/agent.py:139` 都在成功后调 `mark_success`，token 累计到
> `data/llm_pool_state.json`，并能在 `check_llm_pool` 与 `/llm/pool` 看到。真实缺口是维度不全（见下）。

### COST-01 计量口径不全：只有模型维度的 token 累计

- **现状（已经有）**：成功调用会记账 —— `llm/middleware.py:59-66` 取用量、`llm/middleware.py:160`
  `mark_success(spec, pin, pout)`；流式路径同样记（`llm/agent.py:128-139`）。累计字段在
  `llm/pool.py:282-294`（`calls / prompt_tokens / completion_tokens`），落 `data/llm_pool_state.json`，
  `python -m scripts.check_llm_pool` 与 `GET /llm/pool` 都能看到（**模型维度是有的**）。
- **缺什么**：
  - **无时间维度**：只有累计值 → 「今天花了多少」答不出；且 `check_llm_pool --reset` 会把计数一起清零。
  - **无用户/会话维度**：不知道消耗来自谁、来自哪个用途（主回答 / 画像 / 摘要 / 网页答案全混在一起）。
  - **无费用维度**：没有价格表，token ≠ 元。
  - **计数在进程内 + 单文件**（`llm/pool.py:283` 的 threading.Lock），按 OPS-01 扩到多 worker 会各记一份。
- **风险**：这是全项目目前**唯一会在半夜把人叫起来**的部分。模型额度烧完、USDA/网页搜索免费额度打满、
  某个用户脚本刷问答——全是事后发现，且发现时钱已经花了。
- **建议动作**：
  1. 落一张事件表：每笔调用记 `(时间, user, session, 模型, in, out, 用途)`，「用途」至少区分主回答/画像/摘要/网页答案。
  2. `/admin/stats` 加「今日调用次数 / 估算费用 / Top 10 用户」。
  3. 计费累计要与 `--reset`（清理模型可用性状态的动作）**解耦**，不能一键抹掉。
  4. Langfuse 侧已经在记 usage（`observability/tracing.py`），先确认能不能直接导出做账，比自建表便宜。
- **验收**：任意时刻能在后台看到「今天花了多少钱、谁花的」，与账单差距 < 10%；执行 `--reset` 后历史计数仍在。

### COST-02 无用户级配额（登录后 IP 限流失效）

- **现状**：`/chat` 的门槛只有 nginx 层单 IP `10r/m + burst 3`（`00-nutri-graph.conf`），应用层只有 `FOOD_REQUEST_DAILY_LIMIT_PER_USER`、`WEB_ANSWER_USER_HOURLY` 之类零散限制，**/chat 本身没有用户级日配额**。
- **风险**：`AUTH_REQUIRED=true` 之后，一个注册用户用脚本连续换 token 就能绕过 IP 限流把模型额度打穿；反过来，办公室 / 校园出口 IP 后面是一堆正常用户，IP 限流也拦不准真正在刷的那一个人。
- **建议动作**：在 `security/ratelimit.py` 已有的模式下加一个 `chat_quota` 维度（同 ip_freq-count 表即可）：每用户每天 N 次（比如 50），超限返回 429 + `code=quota_exceeded`；匿名身份按 `anon_id` 计；管理员可豁免。
- **验收**：脚本连续请求 100 次，第 51 次起 429；不同用户互不影响。

### QLT-01 评估不在流水线上

- **现状**：`eval/dataset.jsonl` 75 条、基线 faithfulness **0.697** / answer_relevancy 0.498 / abstention 1.0；跑法是手动 `python -m eval.run --run-chain`，不进 CI，也没有「低于阈值就红灯」的门禁。而且 README 自己也警告了：全量评估会往部署库写 `eval-user` 的脏数据。
- **风险**：改 prompt、换模型、调检索阈值——这三件事基本上每周都在做，但没有任何一道闸告诉你「这次改坏了」。faithfulness 0.697 本身也不算高，最容易在无声中被推到 0.5。
- **建议动作**：
  1. 在 CI（self-hosted runner，能连真库）里跑**小子集**（比如每类取 10 条）+ `abstention_correctness` 这类不花 LLM 调用的确定性指标；全量带 LLM 指标那套每周手动跑一次即可。
  2. 给三个硬指标设门禁：faithfulness ≥ 0.65、abstention = 1.0、uncatalogued ≥ 0.75，跌破即红。
  3. 评估默认连 `nutri_graph_test`，把 README 里那条「跑完记得清理」的坑永远堵上。
- **验收**：故意把 `ENTITY_LINK_THRESHOLD` 改成 0.99 提交一个 PR，CI 变红。

### OBS-01 只有 tracing，没有 metrics/告警

- **现状**：`observability/tracing.py` 把 asked/retrieved/answer 写进 Langfuse，metadata 里有 `graph_hit`/`degraded`/`evidence_counts`/`elapsed_ms`——查询很好用。但**全站没有 `/metrics`（grep prometheus 零命中），没有任何告警规则**。
- **风险**：Langfuse 解决的是「事后查案」，不解决「半夜被告知」。典型不可知事故：模型池全部进入冷却 + 兜底也没额度 → 所有用户开始收到「暂时答不了」；USDA key 打满 → 补录静默不产出；Redis 挂了 → /chat/stream 全 503 但 /chat 正常（这种半残状态最难发现）。
- **建议动作**（先止血，不要一上来 Prometheus + Grafana full stack）：
  1. 一个 `/metrics` 端点输出 6 个数就够了：`chat_total{status}`、`chat_latency`、`graph_hit_ratio`、`degraded_ratio`、`abstain_ratio`、`llm_pool_available`、`sms_sent_total`。
  2. 用阿里云云监控（免费额度足够）配 4 条告警：`/health` 非 200、`graph_hit_ratio` 1h 均值 < 0.3、`llm_pool_available = 0`、`sms_sent` 日环比 > 5x。
  3. **把 `/health` 补上 Redis**（现在是 Neo4j/Milvus/PG 三件套，Redis 已经是 SSE 的硬依赖，缺这个检查意味着最典型的半残状态被漏掉）。
- **验收**：手动把 Redis 停掉，2 分钟内收到告警，且 `/health` 变 503。

### KGD-01 Neo4j 测试与生产共图

- **现状**：Neo4j 社区版只有一个库，测试与生产共用。`conftest._graph_guard` 默认跳过写图用例、`ALLOW_GRAPH_TESTS=1` 才放行 —— 护栏是 2026-10-28 那次误删 120 个食材之后才加的。
- **风险**：护栏依赖「人记得不要开这个开关」，CI 里一个手滑就是又一次事故；而且 `--repair-nodes` 只能补回「候选表还在」的节点，孤儿数据救不回来（要用 `--restore`，那是抢救工具不是常态化手段）。
- **建议动作**：
  1. CI / 测试机上起**独立的 Neo4j 容器**（`docker-compose.yml` 复制一份改名 `test`，端口改 `:7688`），从根本上让测试碰不到生产图——这一条收益最高、成本最低。
  2. 若只能共用：给所有测试写入的节点打 `test=1` 属性，并加一条定时清理任务。
  3. 生产图每天一次 `count(n)`，和 PG 侧 merged 候选数对比，自动发现漂移（现有 `scripts/backfill_nutrients.py --repair-nodes --dry-run` 已经是这个能力，只欠定时 + 告警）。
- **验收**：在 CI runner 上跑全量测试（含 `ALLOW_GRAPH_TESTS=1`），生产图节点数不变。

---

## 4. P1：DAU 上千之前必须补

### PERF-01/02 并发天花板与流式线程

- **现状**：`/chat` 是 `def chat(...)`（sync → anyio 线程池），uvicorn `--limit-concurrency 10`；`/chat/stream` 每次请求
  `threading.Thread(daemon=True)` 起一个常驻工作线程（`api/chat.py:158`）。**一次请求最多 3 个线程**：SSE 工作线程 +
  `_defer_profile`（`graph/nodes.py:738-744`，每轮一条，内部再打 LLM）+ `_defer_compress`（`nodes.py:747-760`，每轮一条）。
  三个都要取 PG 连接，且**全仓没有 Semaphore / 线程池约束**（唯一一个池是 `kg/websearch.py:98` 的 `max_workers=1`）。
  SSE 读取侧还有个额外开销：`chatbus.py:108-124` 每次 `blpop` 都新建一条 Redis 连接再 `aclose()`。
- **风险**：needs_v8 的流式改造全量上线后，N 个并发 SSE = 最多 3N 个线程争抢 15 条 PG 连接 → 表现为随机的 `pool_busy` 503，
  而前端只会看到「系统繁忙」，排查时联想不到 SSE。这是 **SSE 上线后才暴露**的风险，提前做。
- **建议动作**：
  1. 给 SSE 加显式并发上限（信号量或在 `open_bus()` 里做配额），超限快速失败并给出「当前排队人数多」的提示，而不是让线程无上限堆积；
  2. `/chat` 改 async（把图调用用 `run_in_threadpool` 包一层即可，不用重写），从一个请求占一个线程变成协程挂起；
  3. 给 `PG_POOL_SIZE` 做一次压测标定，记住 `UVICORN_LIMIT_CONCURRENCY` 和池是一起看的（`config.py:50-65` 的注释已经是很好的文档，别绕开它）。
- **验收**：50 并发 SSE 下，服务不 503、内存不无限增长、p95 延迟有界；压测报告归档。

### PERF-03 全链路零缓存

- **现状**：`grep lru_cache / TTLCache / _cache` 在 `kg/ memory/ rag/ llm/` 下**零命中**。同一句「番茄鸡蛋能一起吃吗」每天被问几百次，每次走：实体链接（Milvus 检索）+ 图查询（Neo4j）+ 一次完整 LLM 生成。
- **风险**：这不是「慢一点」，是**钱**。LLM 部分按次付费，重复问题的高频比例在养生场景里特别高（同一批用户在问同一批热菜）。
- **建议动作**（按性价比）：
  1. **embedding 缓存**：归一化后的问句 → 向量，Redis `GET/SET` 带 TTL（24h）。成本降最快。
  2. **检索结果缓存**：`(实体集合 + intent + evidence_policy) → schema(hash)` → 图命中行缓存分钟级。key 要带上 `INCLUDE_PROVISIONAL` 和 `batch_id` 水位，否则审核通过后会被旧缓存挡住。
  3. **答案缓存**：只对「高频 + 低个性化」问句（无 profile、无 session 上下文）开放，TTL 短 + 命中率低时不启用。这步可以做也可以不做，看 1、2 之后的成本曲线。
- **验收**：重复问同一句第二次，p95 延迟降 > 60%，且 LLM 调用计数不增。

### OBS-02 无请求层标识

- **现状**：全仓无 `X-Request-ID` / 请求级 `trace` 标识；日志格式有 pid/threadName（`utils/logging_config.py:110-113`）。
  ⚠️ **同名陷阱**：仓库里大量 `request_id` 指的是**未收录食材登记编号**（`kg_ingredient_request.id`，如 `kg/request.py:94`），
  是完全不同的东西。做这项时请求标识建议命名 `rid`，不要复用 `request_id`。
- **风险**：一条 `/chat` 会横跨 SSE 工作线程 + 画像线程 + 摘要线程（见 PERF-02），单看 threadName 拼不出先后顺序；「用户说卡住了」时无法定位那一次请求。Langfuse 有 trace id，但与进程日志没有共同键。
- **建议动作**：一个中间件生成 `rid`（uuid4 短版），`contextvars` 注入 → 日志 filter 自动带上 → 响应头回传 → SSE 的每个事件也带；Langfuse trace 的 metadata 挂同一个 rid，两边可互查。
- **验收**：随便抓一次 `/chat`，能在 `backend.log` 里 `grep rid` 拿到完整链路，并跳到 Langfuse 对应 trace。

### KGD-02 来源可追溯性不足

- **现状**：bundle 里存了 `source locator`（URL），但**没有抓取快照、没有 `fetched_at` 之外的版本概念、没有定期复检**。USDA/网页是可能失效的。
- **风险**：一旦某条边被用户质疑（「你说这个数据是哪来的」），URL 404 就答不上来；内容投诉场景下这是硬伤。反过来这也是**差异化机会**——「每条结论都能点进去看原始出处」是很强的产品卖点。
- **建议动作**：抓取时把原始 JSON 落对象存储（MinIO/OSS），`kg_source` 表加 `snapshot_uri` + `checksum`；每季度一次复检任务比对（URL 还活着 + 数值没变）。
- **验收**：任一条补录出来的边，能在后台一键打开它当被抓时的原始快照。

### KGD-03 补录闭环靠人点

- **现状**：全仓 grep `cron / systemd timer / schedule` **零命中**。`scripts/run_fetch` 是纯手动命令；`/admin/kg/requests/{id}/fetch` 也要管理员逐个点。
- **风险**：用户眼里的体验是「我登记了黑豆，两周没动静」——9.1 那一整套登记设计的正向价值就败在最后一公里。而且 `kg_ingredient_request` 的 `hit_count` 热度排序已经做好了，缺的只是让队列自己流动。
- **建议动作**：一个 systemd timer（或 crontab）每 10 分钟跑 `run_fetch --limit 5 --recover 15`，配合已有的 `FOOD_FETCH_DAILY_BUDGET` 防超支；同时给管理员做一个「今日队列深度」的小指标（OBS-01 顺带就覆盖了）。
- **验收**：登记一个新食材，无人干预下 30 分钟内产出 bundle 且出现在待审列表。

### QLT-02 无用户反馈闭环

- **现状**：全仓 grep `feedback / 评分 / 点赞 / 有用` 在接口层**零命中**（命中的都是 bundle 内部的一致性评分，不是用户反馈）。`admin-web` 也没有这个页面。
- **风险**：0.697 的 faithfulness 意味着还有三成 claim 站不住，但**系统不知道是哪三成**。没有反馈就没有负样本，评估集永远长不大。
- **建议动作**（小而关键）：
  1. `/chat/feedback` 接口 + `message` 表加 `feedback(-1/0/1) + feedback_reason`；前端一个「这条没用」按钮（健康类不要做「点赞」，噪声大）。
  2. 后台「差评追问」列表：把反馈为 -1 的 question/answer/evidence 拉出来看，合适的人工补图、合适的补进 eval 数据集。
  3. 「举报」入口（合规手册要求必须有投诉渠道），和反馈分开统计。
- **验收**：第一个月至少回收 50 条负反馈，其中 ≥ 5 条变成新的 eval 用例。

### PRD-01 产品化外壳（这是个路线选择题）

现在的交互壳是手写的 `frontend/index.html` + `app.js`，做链路验证够用，做留存不够。要真对外，三个方向选一个先把漏斗跑通：

| 方向 | 缺什么 | 备注 |
|---|---|---|
| 微信小程序 | 整套前端 | 入口最顺手，但健康类目审核严格，且要备案；后端/API 可直接复用 |
| 移动 App | 整套 + App 备案 + 软著 | 合规手册已给材料清单，工作量最大 |
| **Web H5**（可 PWA） | 适配移动端 + HTTPS + 分享 | **性价比最高**，先把留存跑出来再决定要不要 App |

另外几件便宜且明显能拉动留存的小功能：**历史问答搜索与收藏**、**一天的膳食搭配清单生成**、**输入菜名反查禁忌**（实体链接能力现成，加一层前端皮子即可，不动主链路）。

---

## 5. P2：长期护城河（值得想，不急着做）

1. **证物时钟（evidence decay）**：现在 `evidence_level` 是静态标签。给每条边加 `reviewed_at` + `next_review`，超过窗口自动降一档并在答案里加「这条结论上次核验是 X 年」。市面上几乎没有食疗产品做这个，可信度差异化很明显。
2. **多源交叉验证自动化**：`FOOD_AUTO_APPROVE_MIN_SOURCES=2` 现在永远达不到（单源封顶 0.88）。接第二个权威源（中国食物成分表离线包，`scripts/import_fcd.py` 已有雏形）→ 自动通过率上升 → 人工审核量下降一个数量级。这是规模化的唯一路径。
3. **个性化→群体化**：现有 `user_profile` 是单用户画像。加一层「人群包」（孕早期/透析/IgA 肾病），把禁忌从「查得到」变成「主动提醒」，商业价值也在这里。注意：一旦落到「给特定疾病人群建议」，医疗合规红线立刻抬头（《合规手册》〇节三条铁律），产品口径必须是「科普 + 提示就诊」，不能是「方案」。
4. **成本分层**：高频通用问题走小模型 + 图谱硬约束（几乎不需要大模型发挥），长尾才用大模型。现在的模型池是「按可用性切换」，如果能演进成「按问题难度切换」，单次成本可降一个量级。
5. **知识资产沉淀**：把「什么时候该拒答」这件事本身做成数据集和评测基准——这是这个项目最难被复制的部分，也是简历/PR 上最有辨识度的一项。

---

## 6. 明确建议**不要**现在做的

防止过度工程——这些看起来很美，但在百 DAU 阶段是净负债：

- **微服务 / K8s 改造**：现在就是一个 Python 进程 + 4 个存储。拆服务只会让本地起不来、排查变难。等真的需要横向扩容那天再谈。
- **多租户 / 租户隔离**：没有 B 端客户之前，schema 上加 `tenant_id` 是纯粹的自我折腾。
- **换 Milvus / 换 GraphRAG 框架 / 换 langgraph 版本路线**：当前组合已经在跑且有实测数据，生态兼容成本极高（记忆里 ragas×langchain-community 的补丁就是教训）。
- **Neovis 大图可视化**：memory 里提过借鉴。目前 `kg/subgraph.py` 已经给了 40 节点/60 边的截断子图，够用；Neovis 带来的体积和依赖不划算。
- **自建训练/微调**：图谱 + prompt 已经能覆盖，标注数据不足以支撑微调。
- **把 `requirements.txt` 全量 `==` 锁死**：现在是 `>=x,<y` 的范围约束，配合 Python 3.14 已经踩过一轮编译坑。比起硬锁版本，更划算的是装一台和线上同版本的机器做 wheelhouse 缓存。

---

## 7. 里程碑建议

| 里程碑 | 目标 | 内容 | 出口条件 |
|---|---|---|---|
| **M1 · 敢上线**（约 2 周） | 从「能演示」到「敢挂出去」 | OPS-02 备份 + OPS-03 release.sh + SEC-01 HTTPS + SEC-02 凭据 + SEC-03 consent + OBS-01 六指标四告警（含 /health 补 Redis）+ COST-01/02 计量与配额 + KGD-01 测试隔离 | 域名备案通过；后台能看到当日成本；停 Redis 能告警；一次 restore 演练成功 |
| **M2 · 撑得住**（约 1 个月） | 支撑 1000 DAU | OPS-01 状态外置 + 多 worker + PERF-01/02/03 + QLT-01 CI 门禁 + QLT-02 反馈 + KGD-03 定时任务 + OBS-02 request-id | 50 并发压测通过；评估进 CI；补录队列自动流动 |
| **M3 · 跑得快**（长期） | 规模与差异化 | KGD-02 快照 + 多源自动审核 + 成本分层 + 证物时钟 | 新食材从登记到可用 ≤ 1 天；单条回答成本降一个量级 |

---

## 8. 需要你拍板的三个开放问题

1. **出口选哪个？** Web H5 / 微信小程序 / App —— 直接决定 M2 排期和合规工作量（App 要软著 + 备案，小程序要走另一个审核口径）。**我的建议是先做 Web H5 + PWA**：同一套后端零改动，留存数据跑通再决定要不要 App。
2. **要不要引入第二个权威数据源？** 这是把「人工审核」从瓶颈里解放出来的唯一办法，但需要一次性数据接入工作（中国食物成分表离线包已在 `scripts/import_fcd.py` 有雏形）。不引的话，补录闭环的吞吐上限就是管理员的手速。
3. **月成本红线是多少？** LLM + 短信 + ECS 是全站唯一的可变成本。没有这个数字，COST-01/02 的阈值只能拍脑袋；有了这个数字，`chat_quota`、`FOOD_FETCH_DAILY_BUDGET`、全局短信熔断都能反推出来。

---

## 9. 专题：查询性能与「百万级」容量（追加）

> 追加时间：2026-10-31 · 问题：「假如生产环境配置足够，能支持百万级查询吗？」
> 方法：读数据访问代码（不依赖服务器现状），按查询本身的形态判断复杂度，不做实测。
> **延迟数字均为基于代码结构的估算，不是压测结果。**

### 9.1 先分清「百万级」的两种口径

| 口径 | 现状 | 目标 | 结论 |
|---|---|---|---|
| **数据量**：百万节点 / 千万边 | 1368 食材 / 34 营养素 / 24364 边 | 图长两个数量级 | ✅ **查询模式本身撑得住** |
| **查询量**：日均百万次调用 | 单进程、`--limit-concurrency 10`、单问 ~3–5 s → 约 2–3 QPS 上限 | 日均百万 ≈ 均值 12 QPS、峰值 35–60 QPS | ❌ **差 1–2 个数量级，且加配置基本无效** |

### 9.2 一次 `/chat` 的串行调用链

| 步骤 | 出处 | 典型耗时（估算） | 性质 |
|---|---|---|---|
| 食材识别 LLM（**每问必调**） | `graph/nodes.py:106` → `kg/segment.py:42-56` | ~1.5 s | 外部调用 |
| 实体链接：Python 全表别名扫描 | `kg/normalize.py:260-289` | 0.2 s → 百万别名 **1 s+** | **进程内 CPU** |
| Neo4j 图查询（1–2 跳） | `kg/query.py:28-101` | ~5 ms | 存储 |
| Milvus 降级检索（仅 graph miss 时） | `graph/nodes.py:147-161` | ~20 ms | 存储 |
| 回答生成 LLM | `graph/nodes.py` `generate` | ~3 s | 外部调用 |
| 写 PG + 后台线程 | `nodes.py:725-760` | ~30 ms | 存储 |

**存储合计不到 60 ms。** 剩下的时间全在模型与进程内 CPU 上 —— 这决定了「换更好的机器」几乎不起作用。

### 9.3 为什么数据量没问题

`kg/query.py` 的七个模板形态一致：

```cypher
MATCH (a:Ingredient {canonical_id: $cid})-[r:RECOMMEND_PAIR]-(b:Ingredient)
WHERE r.evidence_type IN $allowed AND NOT b.canonical_name IN $allergens
      AND ($include_provisional OR coalesce(r.provisional, false) = false)
ORDER BY r.evidence_level, r.confidence DESC LIMIT $k
```

**「唯一锁定一个节点 → 沿指定关系类型展开 1 跳」**（`symptom_remedy` 为 2 跳），配合 `kg/constraints.py:20-36` 的
`canonical_id` 唯一约束（自带索引）+ 六个常规索引。代价是 **O(该节点的度)**，与全图规模无关 —— 图从 1400 涨到
100 万节点，这条语句耗时基本不变。

PG 侧索引覆盖同样到位（无需补）：`dict_alias` `(entity_type, alias_norm)` 唯一 + trgm + `canonical_id`；
`message` `(session_id, created_at)` 与 `(session_id, turn_no)`；`session(user_id)`；`sms_send_log` 四个复合索引。

连接复用也做对了：`rag/client.py:24` 的 Milvus 客户端是全局单例、`graph/nodes.py:23` 的 `_query = GraphQuery()`
是模块级共享 driver。每次新建 driver 的只有 admin 路径（`kg/lookup.py:47`）和 `/health`（`api/health.py:23`），
都不在热链路上。

### 9.4 三个真正的瓶颈（都不是加硬件能解决的）

**① 实体链接在 Python 里全表扫别名（致命）**

`kg/normalize.py:260-289`：

```python
for row in sorted(self.alias.all_aliases(), key=lambda r: -len(r["alias_norm"] or "")):
    ...haystack.find(needle, start)...
```

每一句提问都遍历整张别名表，还带一次 `sorted()`。别名表是进程内全量常驻（`get_alias_store()` 单例，见
`normalize.py:35-47`，注释里写着以前每请求 new 一个、全表重读的问题已修）。
估算：**10 万别名 ≈ 70–120 ms/问，100 万别名 > 1 s/问**，纯 Python CPU、单线程、在请求里同步执行。
它直接决定 QPS 天花板，而改配置、加内存都治不了。

修法：把「逐个 find 子串」改成「查索引」—— 按问句生成长度 2–8 的候选子串 → 一次
`WHERE alias_norm = ANY($cands)` 走唯一索引拿回候选；或上 Aho-Corasick 自动机把 N 次 `find` 压成一次。

**② 每问 2 次 LLM 调用（含一次容易被忽略的）**

`graph/nodes.py:106` 的 `_mentions_from_model` → `kg/segment.py:42-56` `_default_llm`，**每问都同步打一次模型做
食材识别**（`thread.join(SPLIT_TIMEOUT_SEC)` 阻塞），之后才是回答生成。LLM 成本与延迟是双份。
日均百万时，模型侧的 QPS / 额度限制会先于数据库成为天花板。

**③ `/kg/subgraph` 的探索查询没有节点标签**

`kg/subgraph.py:290 / 307 / 320 / 334` 全是 `MATCH (a {canonical_id: $cid})-[r]-(b)` —— **不带 label**。
Neo4j 的 unique index 按 label 建，缺 label 用不上索引 → 退化成全节点扫描。1400 节点时无感，百万节点时每个
探索请求都是一次全图扫。

更危险的是 `_PATH_CYPHER`（`subgraph.py:319-331`）：关系类型放在
`WHERE all(r IN relationships(p) WHERE type(r) IN $rels)` 里过滤 → **遍历时无法按类型剪枝**；
而 `Nutrient` 会成为超级节点（现在 34 个营养素挂 2.4 万条边，百万食材时单个营养素度数接近百万），
任何「经营养素中转」的路径查询都会炸。改法：加 label + 把 rel type 写进模式而不是 WHERE。

### 9.5 顺带记下的两处批处理扫描（不紧急）

- `kg/merge.py:401` `MATCH ()-[r]->() WHERE r.batch_id = $bid DELETE r`：无 rel type、无索引 → 全关系扫描。
- `kg/merge.py:407` `MATCH (n) WHERE n.provenance = $bid OR ...`：同样的全节点扫描。

批处理场景当前数据量（2.4 万边）下几秒完成，可接受；百万级会变成分钟级事务。届时给 `batch_id` 加索引即可。

### 9.6 建议的改造顺序

1. **改实体链接**（9.4①）—— 唯一必须做的算法改动，收益最大、其余都围绕它。
2. **给 subgraph Cypher 补 label + 模式内 rel type**（9.4③）—— 小改动，防止线上爆雷。
3. **合并/降级两次 LLM 调用**（9.4②）—— 短问句跳过食材识别模型；高频问题直接缓存。
4. **加缓存（PERF-03）+ 放开多 worker（PERF-01/PERF-02）** —— 先消重复开销，再谈并发。

### 9.7 改造前的三个验证动作（先测量，别猜）

1. `PROFILE` 跑一遍 `_NEIGHBOR_CYPHER` / `_DIRECT_CYPHER` / `_PATH_CYPHER`，确认是否 `AllNodesScan`。
2. 造 10 万条合成别名，实测 `EntityLinker.link_question` 单次耗时（这条可以直接决定要不要改①）。
3. 用 k6 压 `/chat/stream`，拿到真实 QPS 与 p95 曲线，替换本文的估算值。

---

## 10. Q&A 沉淀（本轮问答中补充的事实）

| 议题 | 补充的事实 | 出处 |
|---|---|---|
| SEC-03 的 consent 具体卡在哪 | `consent_profile` 控制两件事：`nodes.py:300` 是否读画像、`nodes.py:849` 是否抽取新的健康属性入 `memory_fact`。改默认值必须和前端弹窗一起发，否则现有用户会集体退化成通用回答 | `api/chat.py:76-79`、`graph/nodes.py:300,849` |
| QLT-01 的基线 | 75 条数据集；`eval/run.py` 的 `--run-chain` 会用真实链路填 answer 再评（否则评的是空回答）；全量评估会往部署库写 `eval-user` 脏数据 | `README` §评估、`eval/run.py:1-12` |
| KGD-01 那次事故的根因 | `rollback()` 曾用 `provenance CONTAINS 'bundle:12'` → 命中 `bundle:120/129/1200-1299`，一次删掉线上 120 个食材节点；同批还有 `merge` 的 `s.run()` 缺 `.consume()` 静默丢 479 节点。现状：`merge.py:407-410` 改成精确匹配、写图测试打 `graph` 标记默认跳过 | `kg/merge.py:388-394`、`tests/conftest.py:90-104` |
