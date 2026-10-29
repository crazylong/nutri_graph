# nutri_graph 需求文档

> 本文档基于 `needs.txt` 重新梳理，仅描述 **nutri_graph 项目自身已实现的功能**，
> 不再保留「参考其它项目」「头脑风暴」「借鉴某项目」等跨项目引用段落（原 `needs.txt` 第 10、11、12 条）。
> 其中第 12 条所衍生的证据分层、canonical id + crosswalk 归一，已固化为本项目图谱核心设计，
> 在本文对应功能需求中按本项目自身能力表述。

## 1. 范围与功能总览

| 需求来源（needs.txt） | 对应功能需求 | 状态 |
|---|---|---|
| #1 项目定位（食疗养生助手） | 第 2 章 项目概述 | 已落地 |
| #2 技术栈 | 第 2 章 技术栈 | 已落地 |
| #3 图谱建模（节点/边/证据分层） | FR-01 知识图谱建模 | 已落地 |
| #4 图谱构建（别名归一 + 候选 + 幂等 MERGE） | FR-02 图谱构建与归一、FR-09 审核闭环 | 已落地（`kg/extract.py` 未接入） |
| #5 检索链路（实体链接 + 意图选跳 + 图谱约束回答） | FR-03 检索链路 | 已落地 |
| #6 边界控制与无据弃答 | FR-04 边界控制 | 已落地 |
| #8 长短期记忆（PostgreSQL） | FR-05 记忆 | 已落地（画像与短期会话；`memory_fact` 未接入） |
| #9 Ragas 评估 + Langfuse 跟踪 | FR-06 评估与可观测性 | 已落地 |
| #13 手机号验证码登录 + OAuth2/JWT + 防刷校验 | FR-07 身份与访问控制 | 已落地 |
| #14 未收录食材登记 + 抓取校验 + 阈值 + 人工审核 | FR-08 未收录登记与抓取、FR-09 审核闭环 | 已落地 |
| #10 参考 msb_assistant 架构 | — 跨项目参考，本文不收录 | 排除 |
| #11 头脑风暴 Langfuse 用法 | — 已并入 FR-06（Langfuse 跟踪） | 排除(内容保留) |
| #12 借鉴 Nutrigraph 三点 | — 成果已并入 FR-01/FR-02，引用段落不收录 | 排除(成果保留) |

## 2. 项目概述

### 2.1 背景与目标
构建一个**食疗养生助手**：用一张带证据等级的食疗知识图谱去约束 LLM，
使「菠菜豆腐不能同吃」这类民间流传说法，与「西柚影响他汀代谢」「痛风慎食高嘌呤」这类有依据的禁忌
在系统里被**结构化区分**——检索时可选择只取有证据的结论，模型只按图谱事实作答，不自行发挥。

### 2.2 技术栈
LangChain / LangGraph / Milvus（向量实体库）/ Neo4j（知识图谱）/ PostgreSQL + pgvector（业务与记忆）/ DashScope（LLM）/ FastAPI（API）/ Langfuse（链路跟踪）/ Ragas（离线评估）。

### 2.3 设计基调
- **图谱约束生成**：检索结果连同关系上的证据等级进入上下文，生成环节不编造、无据即弃答。
- **人工在环**：LLM 抽取的关系与补录的食材，均先落候选/待审，人工确认后才写入主图。
- **幂等可回滚**：每条写入带 `batch_id`，出问题可整批回滚。

---

## 3. 功能需求

### FR-01 食疗知识图谱建模（needs.txt #3）

**目标**：建立可表达「民间说法」与「医学禁忌」两类事实的知识图谱模型。

- **节点（5 类）**
  - `Ingredient` 食材（含性味 nature、≤40 字摘要 summary、长文 detail）
  - `Nutrient` 营养素（含单位 unit、类别 category）
  - `Population` 人群（孕妇 / 痛风患者等，`kind: group | disease`）
  - `Symptom` 症状（含口语与 TCM 术语 aliases、tcm_term）
  - `Taboo` 禁忌对象（他汀类药物 / 高尿酸血症等，`taboo_type: drug | disease_state | cooking | extreme_pair`）
- **关系（5 主类 + 2 辅助）**
  - `CONTAINS_NUTRIENT` 含有营养素（Ingredient→Nutrient）
  - `RECOMMEND_PAIR` 推荐搭配（Ingredient↔Ingredient）
  - `INCOMPATIBLE_WITH` 不宜同食（Ingredient↔Ingredient，**民间说法落这里**）
  - `CONTRA_POPULATION` 人群禁忌（Ingredient→Population，**医学禁忌落这里**）
  - `SUBSTITUTE_FOR` 替代食材（Ingredient→Ingredient）
  - 辅助：`CONTRA_ITEM` 禁忌事项（Ingredient→Taboo）、`HAS_SYMPTOM` 关联症状（Population→Symptom）
- **证据分层（两层正交，统一挂在每条关系上）**
  - `evidence_type`：`guideline | measured | traditional | anecdotal`（哪一类证据）
  - `evidence_level`：`A | B | C | D`（多可信）
  - 配套 `sources`（出处）、`claim_text`（原始断言，用于引用回溯）
  - 检索时按 `evidence_type` 过滤、按 `evidence_level` 排序；可只取有依据的结论。

**验收**：同一食材的「不宜同食」民间说法与「人群禁忌」医学禁忌在图谱中属于不同关系类型，可分别查询与过滤。

---

### FR-02 图谱构建与别名归一（needs.txt #4）

**目标**：保证「西红柿 / 番茄」不建成两个节点；LLM 抽取结果可控地进入主图且不产生重复边。

- **别名归一**：维护 `dict_alias`（跨数据源对照表，非代码内 if-else），节点以 `canonical_id` 唯一标识；口语/别名通过归一表回流。
- **抽取 → 候选 → 确认**：候选关系先落 `kg_candidate`，**人工审核后才 MERGE** 进主图。当前写入候选的是未收录食材补录（FR-08/FR-09），不是 `kg/extract.py`。`TripleExtractor` 仍无调用方，不计入本条已落地范围。
- **幂等写入**：节点 `canonical_id` 唯一约束 + 关系 `relation_key`（源节点 + 关系类型 + 目标节点）确定性 MERGE；入库用 `UNWIND` 批量写入，重跑不长出重复边。
- **审计与回滚**：每条写入带 `batch_id`，可整批回滚（`kg.merge --rollback <batch_id>`）。

**验收**：同一关系重复执行 MERGE，主图中不产生重复边；任意一批次可独立回滚。

---

### FR-03 检索链路与图谱约束回答（needs.txt #5）

**目标**：把用户口语映射到图谱节点，按意图选择跳数，最终回答严格基于图谱事实。

- **实体链接（四级漏斗）**：crosswalk 归一 + `dict_alias` 表 + 别名回流 + 关键词/向量混合检索，将「上火」「犯困」等口语映射到症状/人群节点。
- **意图选跳数**：问搭配走 `食材 → 推荐搭配`；问「能不能一起吃 / 不宜同食」同时查 `INCOMPATIBLE_WITH`；问药物相互作用走 `食材 → 禁忌事项`；问「上火吃什么」用症状节点查关联人群的禁忌食材。
- **图谱约束生成**：查询结果连同关系上的 `evidence_level` 一起进入上下文，模型**只按图里的事实答，不自己发挥**。

**验收**：检索过程可返回 `graph_hit`（是否真走了图谱）；回答中可被检索上下文逐条支撑（faithfulness 可量化）。

---

### FR-04 边界控制与无据弃答（needs.txt #6）

**目标**：长尾信息不污染上下文；图谱无命中时诚实降级，不给推测性建议。

- **长尾处理**：剂量、做法、适用条件这类长尾只存为**节点属性**，回答时只回摘要，不整段塞进上下文。
- **降级检索**：图谱查询 0 命中且向量分数达到阈值时，按文档片段作答，并标明这不是图谱结论。
- **明确弃答**：纯向量仍低分（或检索失败）时，明确回答「**没有找到确切依据**」，不把低分原文交给模型。

**验收**：无依据问题返回弃答话术且 `graph_hit=false`；评估集弃答正确率 `abstention_correctness = 1.0`。

---

### FR-05 长短期记忆（needs.txt #8）

**目标**：在 PostgreSQL 中保存会话记忆与用户画像。

- **短期记忆**：当前会话最近几轮写入 PostgreSQL，下一轮随 `session_id` 进入生成提示词（只帮助理解指代，不当成图谱事实）。
- **长期记忆**：用户画像（过敏 / 忌口）会进 Cypher 过滤。`memory_fact` 的 pgvector 召回尚未接入问答。
- **匿名归并**：未登录领取的匿名身份在登录后归并，聊天记录与画像一并带入账号（见 FR-07）。

**验收**：长期记忆读写经迁移管理（003 记忆向量），非超级用户首次需由 DBA 装 `vector` 扩展。

---

### FR-06 评估与可观测性（needs.txt #9）

**目标**：用 Ragas 做离线评估，用 Langfuse 跟踪线上链路。

- **Ragas 分层评估**：评估集 75 条（有据 30 / 应弃答 30 / 未收录食材 15），分三类评估——
  - 有据样本算 `faithfulness`（基线 0.697）、`answer_relevancy`（0.498）、`llm_context_precision`（0.596）；
  - 弃答样本算 `abstention_correctness`（1.0）；
  - 未收录样本算自建确定性指标 `uncatalogued_handling`（0.80，关键词+正则，不花 LLM）。
  - 混评会把 faithfulness 拖成误导性低分，故必须分层。
- **Langfuse 跟踪**：线上问答链路打点，可在 Langfuse 控制台查看 trace。

**验收**：`python -m eval.run --dataset eval/dataset.jsonl --run-chain` 可产出分层指标报告。

---

### FR-07 手机号验证码登录与访问控制（needs.txt #13）

**目标**：手机号验证码登录，OAuth2 语义令牌，图形校验码防恶意刷短信，匿名身份可归并，管理员鉴权。

- **验证码登录（OAuth2 + JWT）**：注册与登录合并为验证码入口，未注册即建号（不做「未注册」枚举提示）；换取 `access_token` + `refresh_token`。
- **短信通道（双 provider，由 `SMS_PROVIDER` 切换）**
  - `console`：本地开发，不真发短信，验证码落 `sms_code` 表校验（日志可见）。
  - `dypns`：阿里云**号码认证服务（短信认证）**，免签名/免模板/免资质，当天可调通；验证码由平台托管，`CheckSmsVerifyCode` 校验。
- **图形验证码防刷**：`GET /auth/captcha` 返回算术图形验证码（SVG + HMAC 答案存储、一次性），发短信前校验，阻断脚本刷短信。
- **四维频控 + 全局熔断**：同号 60s/次、5 次/时、10 次/天；同 IP 10 次/时；同设备 10 天/次；全局 200 次/天。
- **失败锁定分档**：手机号连续 5 次失败锁 15 分钟；IP 累计 50 次才判批量爆破（避免 NAT 下误伤）。
- **隐私与令牌安全**
  - 手机号三态存储：`phone_hash`（HMAC，唯一索引）/ `phone_enc`（Fernet 密文）/ `phone_mask`（138****8000）。
  - `refresh_token` 只存哈希 + 旋转 + **重放即整族吊销**；`replaced_by` 与登出吊销两种 revoked 状态区分。
  - `AUTH_REQUIRED=true` 时 `/chat` 不再接受前端自传 `user_id`。
- **匿名身份与归并**：匿名身份为服务端签发 JWT（`typ=anon`，`sub` 强制 `anon:` 前缀），登录时带 `anon_token` 触发归并（会话数 / 画像 / 记忆事实）。
- **管理员接口**：整 `/admin` router 挂 `require_admin`，含统计、候选审核、补录审核。

**验收**：`scripts/auth_smoke --in-process` 16 项端到端自检覆盖登录/频控/授权/归并；pytest 覆盖登录与频控用例。

---

### FR-08 未收录食材登记、抓取与校验（needs.txt #14）

**目标**：用户提交库中未收录的食材（如「黑豆」），经抓取与校验后进入待审，低于阈值人工审核，不擅自编造入图。

- **登记三出口（顺序不可乱）**
  1. 非食材（症状/人群/营养素/乱码）→ 直接挡掉，避免污染待审队列。
  2. 疑似别名漏收（黑大豆 ↔ 黑豆）→ 写 `dict_alias_candidate` + 反问，比建新节点便宜且避免重复节点。
  3. 真是新食材 → 写 `kg_ingredient_request`（`food_key` 唯一 → 幂等；`hit_count` → 需求热度）。
  - 复合词先归一到中心词（黑豆粥/炒黑豆/凉拌黑豆 → 黑豆）。中文模糊匹配走 `kg/textsim.py`（包含关系 + 字级编辑距离），不用 pg_trgm。
- **离线抓取 worker（`scripts.run_fetch`，不在请求链路）**
  - 流程：生成检索画像(A) → USDA 检索 → **同名异物核对** → 拉详情 → 四维打分 → 落 `kg_food_bundle`。
  - 同名异物核对：用模型给出的 `identity_keywords`（必须出现）/ `exclude_keywords`（出现即否决）判据区分「黑豆 Glycine max」与「黑菜豆 Phaseolus vulgaris」。
  - **四维打分**：完整性 0.20 + 权威性 0.30 + 一致性 0.30 + 无冲突 0.20。
- **阈值与单源策略**
  - `FOOD_AUTO_APPROVE_MIN_SCORE = 0.9`：低于 0.9 进待审，高于 0.9 审核通过（原「B 模型 0.9 自评」改为四维确定性打分，更可复现）。
  - 单源时一致性恒 0.6 → 得分 0.88 < 0.9，**必然进人工**（单源无法交叉验证，不赌无同名异物）。
  - 低于 0.9 的条目状态置「待审核」，进入待审列表供人工审核。

**验收**：低于阈值的条目不自动入主图；高于阈值经审核后入图；运行记录返回 `food_requests`（request_id / status / hit_count）。

---

### FR-09 审核闭环与主图写入（支撑 #4、#14）

**目标**：所有写主图的操作走唯一入口，保证可审计、可回滚。

- **产物 → 候选 → 主图**：bundle 审核通过只负责「翻译成候选」（`kg_node_candidate` + `kg_candidate`），真正写 Neo4j 的是 `kg.merge`。
- **provisional 单源降级引用（不是藏起来）**：`source_count < FOOD_TRUSTED_MIN_SOURCES`（默认 2）的边打 `provisional`，证据等级封顶 C，上下文标 `[measured/C·单源]`，回答前缀提示「单一来源补录，未做多源交叉验证」。`INCLUDE_PROVISIONAL=false` 时 Cypher 真正过滤（只答硬证据）。
- **主图单一写入口 + 整批回滚**：每条边带 `batch_id`；新食材先查 `dict_alias`/已审核候选命中则挂接不新建；`canonical_id` 序列从 `ING_01000` 起（种子 1~125 一眼可辨）；别名回流在 merge 之后写，名字被占用则跳过并记日志。

**验收**：`scripts.review_bundle approve/reject/merge` 与 HTTP `/admin/kg/bundles` 行为一致；`kg.merge --rollback` 可整批删除。

---

### FR-10 模型池与自动切换（支撑检索/生成的可靠性）

**目标**：LLM 调用统一抽象，模型额度耗尽/限流时自动切换，业务代码不感知具体模型。

- **统一调用层**：业务走 `llm.invoke_model()`，内部用 LangChain 官方 `AgentMiddleware`（`wrap_call_model`）实现按池顺序重试。
- **池策略（`.env` 配置，代码不写死额度/到期）**
  - 额度用尽（403 `AllocationQuota`/`insufficient_quota`）→ 移出池；
  - 其它错误（429 / 5xx / 超时 / 模型不存在）→ 进冷却队列，到点回池；
  - auth/请求错误（401 / 400）→ 不切换、直接抛。
  - 池全不可用 → 退到 `LLM_MODEL` 兜底；兜底也失败 → 抛 `LLMUnavailableError`，生成节点降级「暂时答不了」，不编答案。
- **状态落盘**：`data/llm_pool_state.json`，重启不重复撞已移除模型；当前成功模型「粘住」避免输出抖动。

**验收**：`scripts.check_llm_pool [--probe|--reset]` 与 `GET /llm/pool` 可查看池状态。

---

## 4. 非功能性需求

- **部署与常驻**：`docker compose up -d` 起 Neo4j / Milvus / PostgreSQL / Langfuse；生产用 `deploy/install.sh`（systemd 常驻 + nginx 反代静态页与 API）一键部署。
- **安全**
  - 隐私：手机号 HMAC 哈希 + Fernet 加密 + 掩码；令牌哈希存储 + 旋转 + 重放吊销。
  - 防刷：图形验证码 + 四维频控 + 全局熔断。
  - 鉴权：`/admin` 统一 `require_admin`；`AUTH_REQUIRED` 可强制登录。
- **可测试性**：pytest 约 90 项（PG + Neo4j，缺哪个跳过哪组）；端到端 `scripts.auth_smoke --in-process` 16 项（进程内、不占端口）。测试默认仅对库名含 `test` 的库放行 TRUNCATE，避免破坏补录审计完整性。
- **最小前端**：`frontend/`（index.html + assets，无构建步骤，nginx 直接吐），含验证码 UI 与基础问答页。

---

## 5. 需求追踪矩阵（needs.txt → 功能）

| needs.txt | 是否收录 | 落点 |
|---|---|---|
| #1 项目定位 | 收录 | 第 2.1 节 |
| #2 技术栈 | 收录 | 第 2.2 节 |
| #3 图谱建模 | 收录 | FR-01 |
| #4 图谱构建与归一 | 收录 | FR-02、FR-09 |
| #5 检索链路 | 收录 | FR-03 |
| #6 边界控制与弃答 | 收录 | FR-04 |
| #8 长短期记忆 | 收录 | FR-05 |
| #9 评估 + Langfuse | 收录 | FR-06 |
| #11 Langfuse 头脑风暴 | 内容并入 | FR-06（Langfuse 跟踪） |
| #12 借鉴 Nutrigraph 三点 | 成果并入 | 证据分层→FR-01；canonical+crosswalk→FR-02；Neovis 浏览未实现，不收录 |
| #13 手机号验证码 + JWT + 防刷 | 收录 | FR-07 |
| #14 未收录登记 + 抓取校验 + 阈值 + 人工审核 | 收录 | FR-08、FR-09 |
| #10 参考 msb_assistant 架构 | 排除 | 跨项目参考，非本项目功能 |
