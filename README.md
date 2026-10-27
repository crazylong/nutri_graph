# nutri_graph —— 食疗养生助手

用一张**带证据等级的食疗知识图谱**去约束 LLM，让「菠菜豆腐不能同吃」和「西柚影响他汀代谢」在系统里不是同一种东西。

技术栈：LangChain / LangGraph / Milvus / Neo4j / PostgreSQL(pgvector) / DashScope / FastAPI / Langfuse / Ragas

## 核心设计

- **证据分层（两层正交）**：`evidence_type`(guideline|measured|traditional|anecdotal) × `evidence_level`(A|B|C|D)，所有关系统一注入，检索时按类型过滤、按等级排序。
- **结构上区分两类禁忌**：民间说法走 `INCOMPATIBLE_WITH`（食材↔食材），医学禁忌走 `CONTRA_POPULATION`（食材→人群）/ `CONTRA_ITEM`（食材→禁忌对象）。
- **候选表人工确认**：LLM 抽取结果先落 `kg_candidate`，人工审核后才 MERGE 主图。
- **幂等写入**：节点 `canonical_id` 唯一约束 + 关系 `relation_key` 确定性 MERGE + `batch_id` 回滚。
- **口语→节点**：crosswalk 归一 + `dict_alias` 表 + 别名回流，四级实体链接漏斗。
- **无据弃答**：图 0 命中降级纯向量，仍低分则明确答「没有找到确切依据」。

详细设计见 `docs/plan.md`。

## 模型池与自动切换

LLM 调用统一走 `llm/` 层，业务代码不需要知道「这次用的是哪个模型」。选择与切换用的是 **LangChain 官方的中间件机制**，不是自研调用循环：

```python
from llm import invoke_model
answer = invoke_model(prompt).content          # 内部是 Agent + wrap_call_model 中间件

# 也可以自己装配官方 Agent，把中间件挂上去
from langchain.agents import create_agent
from llm import primary_model, wrap_call_model
agent = create_agent(model=primary_model(), middleware=[wrap_call_model])
```

`wrap_call_model` 是 `AgentMiddleware` 子类，实现了官方的 `wrap_model_call` / `awrap_model_call` 双钩子：按池顺序取候选，用 `request.override(model=...)` 把下一个模型交给 handler 重试。需要原生 `BaseChatModel` 的场合（离线评估、Ragas）用 `pool_chain()`，即 LangChain 原生的 `head.with_fallbacks(rest)`。

池成员只在 `.env` 里配，代码里不写死额度/到期——百炼会不定期送额度，写死必然过期：

```dotenv
# 顺序即优先级，第一个是主模型；只收「大语言模型」，视觉/视频模型协议不兼容别放进来
LLM_MODEL_POOL=deepseek-v4-flash-0731,qwen3.8-max,qwen3.8-2.4t-a95b,deepseek-v4-pro-0813
# 兜底模型：池内全部不可用时才用，填池外的模型最合适
LLM_MODEL=deepseek-v4.1-flash-1017
```

三种失败，三种处置：

| 情况 | 处置 | 怎么恢复 |
|---|---|---|
| **额度用尽**（403 `AllocationQuota` / `insufficient_quota`） | 直接**移出池**（`removed`），不再调用 | `--reset` 让它回池 |
| **其它错误**（限流 429 / 5xx / 超时 / 模型不存在） | 进**冷却队列**，到点自动回池 | 自动 |
| **auth / 请求本身有问题**（401 / 400） | 不切换、不记状态，直接抛 | 修 key 或改请求 |

池内模型全部不可用（都被移除 / 都在冷却）时，自动退到 `.env` 的 `LLM_MODEL` 兜底；兜底也失败才抛 `LLMUnavailableError`，`generate` 节点降级成「暂时答不了」，不会编答案。

- **顺序**：`.env` 里的先后即优先级；当前成功的模型会被「粘住」，避免同一进程内来回换导致输出风格/JSON 结构抖动。
- **状态落盘** `data/llm_pool_state.json`，重启不会再去撞已经移出池的模型。

```bash
python -m scripts.check_llm_pool            # 看池状态：谁在用、谁被移除/冷却、兜底是谁
python -m scripts.check_llm_pool --probe    # 每个模型发一次最小请求，实测可用性
python -m scripts.check_llm_pool --reset    # 额度补上了，让移出池的模型全部回池
curl http://localhost:8810/llm/pool         # 运行时视图
```

## 快速开始

```bash
# 1. 起依赖（Neo4j / Milvus / PostgreSQL / Langfuse）
docker compose up -d

# 2. 配置环境变量
cp .env.example .env
# 编辑 .env，填入 DASHSCOPE_API_KEY（必填）
# 如需 Langfuse，到 http://localhost:3000 注册后填 PUBLIC/SECRET KEY

# 3. 安装 Python 依赖
python -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt

# 4. 初始化存储（所有表都由 migrations 管，一条命令，幂等）
python -m scripts.migrate            # 001 身份 · 002 业务表 · 003 记忆向量 · 004-007 补列/补录
#   ⚠️ 003 依赖 pgvector。非超级用户跑时，先由 DBA 装一次（pgvector 未标 trusted）：
#   psql -U postgres -d nutri_graph -c 'CREATE EXTENSION IF NOT EXISTS vector'
#   （迁移器会直接提示这条命令，不会只丢一个栈）
python -m kg.constraints             # Neo4j 约束/索引
python -m scripts.load_crosswalk     # 别名归一表（360 条）
python -m scripts.load_seed          # 演示数据（165 条：125 节点 + 40 边）
#   ⚠️ NEO4J_PASSWORD 必须与 docker 里的 NEO4J_AUTH 一致，否则 seed 直接抛 AuthError

# 5. 启动服务
python app.py                        # http://localhost:8810
```

生产部署（systemd 常驻 + nginx 反代静态页与 API）见 **[docs/deploy.md](docs/deploy.md)**，
一条命令：

```bash
./deploy/install.sh
```

## 身份与访问控制（手机号验证码登录）

注册与登录合并为一个入口：验证码登录，未注册即建号（不做「该手机号未注册」的枚举提示）。

```bash
# 1. 发验证码（console 模式不真发短信，验证码打在日志里；DEBUG=true 时也会回填到响应）
curl -X POST http://localhost:8810/auth/sms \
  -H 'Content-Type: application/json' -d '{"phone":"13800138000"}'

# 2. 换令牌（OAuth2 语义：access_token + refresh_token）
curl -X POST http://localhost:8810/auth/token \
  -H 'Content-Type: application/json' -d '{"phone":"13800138000","code":"123456"}'

# 3. 取当前用户
curl http://localhost:8810/auth/me -H "Authorization: Bearer $ACCESS"

# 4. 刷新（旋转）/ 登出 / 注销
curl -X POST http://localhost:8810/auth/refresh -H 'Content-Type: application/json' \
  -d "{\"refresh_token\":\"$REFRESH\"}"
curl -X POST http://localhost:8810/auth/logout  -H 'Content-Type: application/json' \
  -d "{\"refresh_token\":\"$REFRESH\"}"
curl -X DELETE http://localhost:8810/auth/me -H "Authorization: Bearer $ACCESS"
```

**短信通道有两个 provider**，由 `SMS_PROVIDER` 切换：

| 值 | 用途 | 验证码归属 |
|---|---|---|
| `console` | 本地开发，不真发短信 | 我方生成，落 `sms_code` 表校验 |
| `dypns` | 阿里云**短信认证**（号码认证服务） | 平台生成托管，调 `CheckSmsVerifyCode` 校验 |

> 个人实名认证账号**不要走「短信服务 SMS」**：自用签名过不了运营商签名实名制报备，
> 且官方已停止受理「测试或学习 / 已备案网站 / 公众号或小程序」等签名来源。
> 走 `dypns` 免资质、免签名、免模板，当天可调通；代价是短信抬头是平台赠送签名，不可自定义。
> 生产还需 `pip install alibabacloud_dypnsapi20170525`（已注释在 requirements.txt）。

**安全设计**（细节见 `docs/plan-auth-and-dynamic-kg.md`）：

- 手机号三态存储：`phone_hash`（HMAC，唯一索引）/ `phone_enc`（Fernet 密文）/ `phone_mask`（138****8000）
- 四维频控 + 全局熔断：同号 60s/次、5 次/时、10 次/天；IP 10 次/时；设备 10 次/天；全局 200 次/天
  —— 平台不代管「刷别人手机号」的短信轰炸，这层必须自己有
- 验证码失败锁定分两档：**手机号 5 次**锁 15 分钟；**IP 50 次**才判批量爆破
  （若 IP 也按 5 次锁，办公室/校园 NAT 后面的人会被彼此连累锁死）
- refresh token 只存哈希 + 旋转 + **重放即整族吊销**
- 两种 revoked 分得开：`replaced_by` 有值 = 被旋转换掉了（真重放，吊销整族）；
  为空 = 登出/下线吊销的（正常失效，提示重新登录）。混成一种会让「点了退出」
  下次回来显示「检测到异常登录」
- `AUTH_REQUIRED=true` 后 `/chat` 不再接受前端自传的 `user_id`，匿名身份也进不来

线上是 `AUTH_REQUIRED=true`。短信还是 `console` 通道（不真发），而 `DEBUG=false` 时
接口不回填验证码（回填等于任何人可用任意手机号登录），所以验证码只在服务日志里：

```bash
python -m scripts.latest_code                      # 最近一条
python -m scripts.latest_code --phone 13800138000  # 指定号码
```

### 匿名身份与登录后归并

未登录也能问，登录后把聊过的内容和画像带过去 —— 否则「我海鲜过敏」一登录就丢。

```bash
# 领取匿名身份（服务端签发，不能由前端自己编）
curl -X POST http://localhost:8810/auth/anon
#   → {"anon_id":"anon:...","anon_token":"...","expires_in":2592000}

# 用 anon_token 聊天（Authorization: Bearer <anon_token>），登录时带上它触发归并
curl -X POST http://localhost:8810/auth/token \
  -H 'Content-Type: application/json' \
  -d '{"phone":"13800138000","code":"123456","anon_token":"<anon_token>"}'
#   → {"merged":{"sessions":3,"profile":true,"memory_facts":0,...}}
```

匿名身份必须是服务端签发的 JWT（`typ=anon`，`sub` 强制 `anon:` 前缀）。若改成前端传 id，
任何人填别人的 id 就能把对方的过敏史并进自己账号。

### 管理员接口

整个 `/admin` router 挂 `require_admin`，新加接口不会漏鉴权。

```bash
curl http://localhost:8810/admin/stats -H "Authorization: Bearer $ADMIN_ACCESS"
#   → 用户数 / 今日短信量 vs 日配额 / 校验失败数 / 待审候选数 / 待审补录 / 未收录解决率

curl "http://localhost:8810/admin/kg/candidates?status=pending&limit=50" \
  -H "Authorization: Bearer $ADMIN_ACCESS"
curl -X POST http://localhost:8810/admin/kg/candidates/12/approve -H "Authorization: Bearer $ADMIN_ACCESS"
curl -X POST http://localhost:8810/admin/kg/candidates/13/reject  -H "Authorization: Bearer $ADMIN_ACCESS" \
  -H 'Content-Type: application/json' -d '{"reason":"来源单一"}'
```

`kg_candidate` 已由 `002_business.sql` 建（迁移管），表不存在时返回 503 并提示该跑哪条命令。
补录审核的接口在 `/admin/kg/bundles`，见下文「审核闭环」。

### 未收录食材的登记

图谱里没有的食材不硬猜，而是登记 + 明确回话：

```bash
# 聊天时自动登记（答案里会说明已记录、给出编号）
curl -X POST http://localhost:8810/chat -H 'Content-Type: application/json' \
  -d '{"question":"黑豆和什么一起吃好？"}'

# 也可以直接提交 / 查自己提交的进度
curl -X POST http://localhost:8810/kg/food/request \
  -H 'Content-Type: application/json' -d '{"food":"黑豆粥"}'
curl http://localhost:8810/kg/food/request/12 -H "Authorization: Bearer $ACCESS"
```

三条出口，顺序不能乱：

| 情况 | 动作 | 为什么 |
|---|---|---|
| 不是食材（症状 / 人群 / 营养素 / 乱码） | 直接挡掉 | 否则待审队列会被污染 |
| 疑似别名漏收（黑大豆 ↔ 黑豆） | 写 `dict_alias_candidate` + 反问 | 比建新节点便宜得多，也避免重复节点 |
| 真是新食材 | 写 `kg_ingredient_request` | `food_key` 唯一 → 幂等；`hit_count` → 需求热度 |

复合词先归一到中心词（黑豆粥 / 炒黑豆 / 凉拌黑豆 → 黑豆），否则一个东西会开出三条抓取任务。

中文模糊匹配**不用 pg_trgm**：实测 `similarity('黑大豆','黑豆') = 0.000`，
中文走 `kg/textsim.py` 的包含关系 + 字级编辑距离。

### 抓取与校验（离线 worker）

抓取**不在请求链路里跑**——一次要 2 次外部 API + 可能一次模型调用，10~40 秒；
做成独立命令后可重跑、可挂定时任务、可单步调试。

```bash
python -m scripts.run_fetch --status        # 看任务/产物/当日预算
python -m scripts.run_fetch --limit 1       # 处理 1 个排队任务
python -m scripts.run_fetch --task-id 3     # 只跑指定任务，便于调试
python -m scripts.run_fetch --recover 15    # 把卡在 running 超 15 分钟的任务放回队列
```

流程：生成检索画像(A) → USDA 检索 → **同名异物核对** → 拉详情 → 四维打分 → 落 bundle。

在同名异物这一环上有个关键取舍：**分类判断不了它是谁**。
实测「黑豆」（Glycine max）和「黑菜豆」（Phaseolus vulgaris）在 USDA 里
同属 `Legumes and Legume Products`，且 SR Legacy 记录不返回 `scientificName`。
所以判据是「A 模型」给出的两个词表：

| 字段 | 作用 | 黑豆 |
|---|---|---|
| `identity_keywords` | 描述里**必须**出现的物种独有词 | `soybean` |
| `exclude_keywords` | 出现即否决的同名异物词 | `black` / `turtle` / `mung` … |

即使模型把黑豆翻成 "black beans"，`soybean` 这一条也会把
"Beans, black, mature seeds, raw" 拦下。

**评分**：`完整性 0.20 + 权威性 0.30 + 一致性 0.30 + 无冲突 0.20`。
只接一个来源时一致性恒为 0.6 → 得分 0.88 < 0.9，**必然进人工**。
这是刻意的：单源无法交叉验证，凭一个来源自动写主图就是在赌它没有同名异物。

> `FDC_API_KEY` 默认 `DEMO_KEY`（30 次/小时、50 次/天，全站共享，只够验证链路）。
> 正式用请到 api.data.gov 免费申请，1000 次/小时。

### 审核闭环（产物 → 主图）

```bash
python -m scripts.review_bundle list                  # 待审清单
python -m scripts.review_bundle show 3                # 全部字段 + 逐项营养素 + 来源 locator
python -m scripts.review_bundle approve 3             # 通过 → 翻译成候选 → 合并进主图
python -m scripts.review_bundle approve 3 --items 12,13   # 只采用部分项（逐条审）
python -m scripts.review_bundle approve 3 --no-merge  # 只生成候选，稍后 merge
python -m scripts.review_bundle reject 3 --reason "物种不对，是黑菜豆"
python -m scripts.review_bundle merge                 # 补做合并（上次 Neo4j 挂了之后）
```

同样的能力有 HTTP 版（`/admin/kg/bundles`、`/admin/kg/bundles/{id}/approve|reject`），
整个 router 挂 `require_admin`。

**主图只有一个写入口**：审核只负责「把 bundle 翻译成候选」
（`kg_node_candidate` 节点 + `kg_candidate` 边），真正写 Neo4j 的是 `kg.merge`。
这样每条边都带 `batch_id`，出问题能整批回滚（`kg.merge --rollback bundle:3`）。

三个必须做对的细节：

| 细节 | 处理 |
|---|---|
| 新食材和已有食材同名 | 先查 `dict_alias` 再查已审核候选，命中就**挂过去**（`identity=existing_alias`），不新建节点 |
| `canonical_id` 分配 | 序列从 `ING_01000` 起（种子是 1~125，一眼看出是补录），先查后分配，merge 失败重试不留孤儿 |
| 别名回流 | **在 merge 之后写**；某名字已被别的节点占用就跳过并记日志 —— 宁可查不到，也不能一个名字指向两个节点 |

#### provisional：单源数据「降级引用」，不是「藏起来」

| 概念 | 含义 | 处理 |
|---|---|---|
| 人工审核 | 这是不是那个东西 | 没通过**根本不进主图** |
| 交叉验证 | 这个数对不对 | `source_count < FOOD_TRUSTED_MIN_SOURCES` → 边打 `provisional` |

provisional 的边证据等级封顶 C（多源才给 B），上下文标 `[measured/C·单源]`，
回答前缀换成「单一来源补录，未做多源交叉验证」。

不默认隐藏的理由：刚审核通过却查不到，用户会以为仍是「未收录」，转头再登记一次 ——
9.1 花力气避免的重复就白做了。真正的风险不是它在图里，而是被当成硬证据。
需要「只答硬证据」时把 `INCLUDE_PROVISIONAL` 设成 `false`，Cypher 里是真的过滤。

### 测试

```bash
# 90 项 pytest（PostgreSQL + Neo4j；哪个连不上就跳过对应那一组，不会红一片）
PG_DSN=postgresql://... NEO4J_PASSWORD=... python -m pytest -q tests

# 16 项端到端自检（进程内跑，不占端口，不依赖 pytest）
DEBUG=true python -m scripts.auth_smoke --in-process
```

> ⚠️ 这套用例会 **TRUNCATE 业务表**（`kg_food_bundle` / `kg_ingredient_request` / `app_user` …），
> 但清不到 Neo4j 和 `dict_alias`。对着有数据的库跑一遍，图里会留下「节点在、审计记录没了」的孤儿数据
> —— 这正好破坏补录设计里最不能破的那条（每条数据都要能回答「谁、什么时候、凭什么加进来的」）。
>
> 所以默认只在库名含 `test` 时放行，否则整组跳过并说明原因：
>
> ```bash
> # 推荐：指一个独立库
> PG_DSN=postgresql://nutri:nutri@localhost:5432/nutri_graph_test python -m pytest -q tests
>
> # 确认可以清（比如演示库）才显式放行
> TEST_DB_OK=1 python -m pytest -q tests
> ```

## 验收

```bash
# 健康探活（4 个存储全绿）
curl http://localhost:8810/health

# 问答
curl -X POST http://localhost:8810/chat \
  -H 'Content-Type: application/json' \
  -d '{"question":"痛风能吃海鲜吗","user_id":"u1"}'
```

返回里除了 `answer` / `basis` / `evidence_counts`，还有两个字段前端要用：

| 字段 | 用途 |
|---|---|
| `graph_hit` | 这次是不是真走了图谱（false = 降级或弃答，可据此换展示样式） |
| `food_requests` | 未收录时结构化的登记结果（`request_id` / `status` / `hit_count`），不用从话术里抠编号 |

## 候选审核与合并

```bash
python -m kg.review list --status pending      # 列出待审（LLM 抽出的关系）
python -m kg.review approve --ids 1,2,3        # 批准
python -m kg.review reject --ids 4             # 驳回
python -m kg.merge                              # 合并 approved 进主图（先节点、后边）
python -m kg.merge --batch-id bundle:3          # 只合并某个补录批次
python -m kg.merge --rollback bundle:3          # 整批回滚（删该批次写的边 + 它新建的节点）
```

补录进来的食材走的是同一条合并路径，审核命令见上文「审核闭环」。

## 评估

```bash
# 推荐：先用真实链路跑一遍，再评估（需要 Neo4j / Milvus / LLM 就绪）
python -m eval.run --dataset eval/dataset.jsonl --run-chain

# 只评数据集里已有的 response（dataset.jsonl 里 response 是空串时会被警告）
python -m eval.run --dataset eval/dataset.jsonl
```

评估集 75 条，三类：**有据 30 / 应弃答 30 / 未收录食材 15**。

评估是**分层**的（`eval/run.py`）：faithfulness 等 LLM 指标只对有据样本算，
弃答题跑 `abstention_correctness`、未收录题跑 `uncatalogued_handling`。
混在一起评会把 faithfulness 拖成一个误导性的低分 —— 弃答/登记话术没有可验证的 claim。

当前基线（75 条全量，真实链路 + ragas 0.4.3，DashScope 评估）：

| 指标 | 分数 | 测什么 |
|---|---|---|
| `faithfulness` | **0.697** | 有据回答的 claims 是否都能在检索上下文里找到支撑 |
| `answer_relevancy` | 0.498 | 回答与问题的相关度 |
| `llm_context_precision` | 0.596 | 检索到的上下文是否真被用上 |
| `abstention_correctness` | **1.0** | 没依据的说法有没有拒答 |
| `uncatalogued_handling` | **0.80** | 库里没有的食材：有没有说「暂未收录」、有没有编造数值、有没有做疗效断言 |

后两个是自建指标，纯确定性规则（关键词 + 正则），不花 LLM 调用 ——
「有没有编造数字」交给模型反而不可复现。

> 跑全量评估会写 `eval-user` 的 message/session 到部署库，跑完记得清理；
> 评估 LLM 用 `max_tokens=LLM_MAX_TOKENS_CEILING`（默认 2048 会让 Faithfulness 的
> statement 生成被截断，报 LLMDidNotFinish）。

## 目录结构

```
nutri_graph/
├── app.py              # FastAPI 装配（路由都在 api/ 下，这里只剩 include_router）
├── config.py           # 集中配置
├── graph/              # LangGraph（state/nodes/workflow/boundary）
├── kg/                 # 图谱域
│   ├── schema/constraints/normalize/textsim     # 模型、约束、归一、中文相似度
│   ├── extract/merge/query/review               # 抽取、主图写入口、查询、关系审核
│   ├── request/usda/draft/verify/fetch          # 未收录登记、抓取与校验（9.1/9.2）
│   ├── nutrients/promote                        # 营养素映射、审核闭环（9.3）
├── rag/                # Milvus（client/embed）
├── memory/             # 记忆（short_term/long_term/anonymous_merge）
├── security/           # 身份与访问控制（sms/ratelimit/tokens/crypto）
├── api/                # 路由层（auth/deps/admin/chat/health/system/kg）
├── migrations/         # 001 身份 · 002 业务 · 003 记忆向量 · 004-007 补列/补录/审核
├── observability/      # Langfuse（tracing）
├── eval/               # Ragas（metrics/run/dataset）
├── tests/              # pytest（90 项：登录/频控/授权/归并/登记/抓取/审核）
├── scripts/            # migrate/load_crosswalk/load_seed/run_fetch/review_bundle/check_fdc/auth_smoke
├── frontend/           # 最小前端（index.html + assets，无构建步骤，nginx 直接吐）
├── deploy/             # systemd unit / nginx 配置 / install.sh
├── data/               # crosswalk/ seeds/
└── docs/               # plan.md / plan-auth-and-dynamic-kg.md / plan-phase9-uncatalogued.md / deploy.md
```
