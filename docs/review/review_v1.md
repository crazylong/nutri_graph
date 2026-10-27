# nutri_graph 代码质量评估 v1

> 对照 `docs/requirements.md`（FR-01 至 FR-10）做的只读审查。
> 范围是当前仓库源码与测试，不含线上数据，未改代码。
> 日期：2026-10-27。

## 1. 结论

写图、审核和登录这条线是扎实的；问答检索还没有把需求里的图谱约束接完。

十项需求落地均分 **3.8 / 5**。FR-08 补录和 FR-09 审核为 5 分，FR-03 检索链路为 2 分。

审核、补录、登录有测试、有单一写入口，失败时会关掉而不是编答案。项目要区分的「民间不宜同食」和「医学禁忌」，在 Schema 和 `kg.merge` 里是两类边，在问答 Cypher 里只查得到医学禁忌和推荐搭配。检索、弃答、记忆读取是后续改动的优先面。

应用代码大约 8 千行，测试大约 1.8 千行。

## 2. 各需求得分

分数是这次审查的判断，来自验收句与当前 Python 调用关系的对照，不是跑出来的指标。

| 需求 | 落地 | 扎实度 | 说明 |
|---|---:|---:|---|
| FR-01 图谱建模 | 4 | 5 | 五类节点、七类关系、证据四件套在 `kg/schema.py` 里完整。`INCOMPATIBLE_WITH` 能写入，查询模板不读它。 |
| FR-02 构建与归一 | 4 | 4 | `canonical_id`、`relation_key`、`UNWIND` MERGE、`batch_id` 回滚在 `kg/merge.py`。`kg/extract.py` 的三元组抽取没有调用方。 |
| FR-03 检索与图谱约束回答 | 2 | 3 | 子串链接和关键词意图在跑。不宜同食、症状问法、在线接地检查没有接到问答路径。 |
| FR-04 边界与无据弃答 | 3 | 3 | 图与向量都空时会弃答，已收录但缺证据有单独话术。向量命中后仍把档位写成无据。 |
| FR-05 长短期记忆 | 3 | 3 | 本轮会落库，过敏画像会进 Cypher，匿名归并有测试。历史不进提示词，pgvector 事实召回未接入。 |
| FR-06 评估与可观测性 | 4 | 4 | Ragas 分层评估在 `eval/`。Langfuse 只有整次 `/chat` 一条 trace。 |
| FR-07 身份与访问控制 | 4 | 5 | 验证码登录、令牌旋转、图形验证码、手机号三态存储有测试。设备维频控和 IP 取值有口子。 |
| FR-08 未收录登记与抓取 | 5 | 5 | 三出口、同名异物核对、四维打分、单源封顶都有测试。 |
| FR-09 审核闭环 | 5 | 5 | bundle → 候选 → `kg.merge` 单入口；provisional 与幂等 MERGE 有测试。 |
| FR-10 模型池 | 4 | 4 | 额度移除、冷却、兜底、状态落盘已实现。没有单测；部署固定单 worker。 |

落地均分：(4+4+2+3+3+4+4+5+5+4) / 10 = 3.8。

5 分表示运行时行为与验收一致且有测试。1 分表示模块在，主路径未调用。

## 3. 问题清单

### P0 检索主路径

这些会让「图谱约束回答」在常见问法上落空。

**1. 不宜同食边写得进图，问答查不到**

- 位置：`kg/query.py`，`kg/merge.py`，`kg/schema.py`
- 需求：FR-01、FR-03
- 查询模板覆盖搭配、人群禁忌、禁忌事项、营养、替代。`INCOMPATIBLE_WITH` 能经 merge 写入，没有任何 Cypher 会读它。问句里的「一起吃」命中搭配意图后只查 `RECOMMEND_PAIR`。

**2. 症状问法走不到症状查询**

- 位置：`kg/query.py` 的 `symptom_remedy`，`graph/nodes.py` 的 `graph_query_node` 与 `route_after_link`
- 需求：FR-03
- `symptom_remedy` 按 `Symptom` 节点匹配。`graph_query` 只对 `Ingredient` 发查询。问句里没有食材时，`route_after_link` 直接去向量降级，这条 Cypher 不会执行。模板收集的是人群禁忌下要避开的食材。

**3. 向量降级和弃答叠在同一条路径上**

- 位置：`graph/nodes.py` 的 `generate_node`、`vector_fallback_node`
- 需求：FR-04
- 向量块分数达到 `CHUNK_THRESHOLD` 之后，生成档位仍被写成 `none`，提示词要求弃答，同时把文档原文放进「图检索事实」。低于阈值则直接弃答。两条路径都不会按向量内容作答。

**4. 在线接地检查是空节点**

- 位置：`graph/nodes.py` 的 `verify_node`，`graph/workflow.py`
- 需求：FR-03、FR-04
- 节点挂在工作流上。答案和上下文都非空时函数直接 `return {}`。注释写明是演示级检查。线上没有断言级接地。离线 faithfulness 在 `eval/`，不在请求链路里。

### P1 写了但主链路没调用

**5. 短期记忆只写不读**

- 位置：`memory/short_term.py`，`graph/nodes.py` 的 `memory_write_node`
- 需求：FR-05
- 问答链路只调用 `save_round`。`load_context` 与 `summarize_if_needed` 没有调用方。生成提示词不含上一轮。`normalize_node` 的注释写着多轮指代由 memory 补全，提示词里没有这段历史。

**6. 长期事实召回未接入**

- 位置：`memory/long_term.py`
- 需求：FR-05
- `UserProfile` 会在图查询前加载，过敏和忌口会进 Cypher。`LongTermMemory.add_fact` / `search` 没有调用方，pgvector 事实召回未接入。匿名归并（会话、画像）有测试，在 `memory/anonymous_merge.py`。

**7. 问答实体链接只做子串匹配**

- 位置：`kg/normalize.py` 的 `link_question`，`graph/nodes.py` 的 `link_node`
- 需求：FR-03
- `link_question` 做别名最长子串匹配。模糊和向量在 `link()` 上，问答不走它。向量阈值 `0.82` 写死，未读 `ENTITY_LINK_THRESHOLD`。每次请求新建 `AliasStore`，实例上的缓存随请求丢弃，全表别名每问重读。

**8. LLM 三元组抽取没有入口**

- 位置：`kg/extract.py`
- 需求：FR-02
- `TripleExtractor` 没有调用方。活的入图路径是 USDA bundle → 候选 → `kg.merge`。需求里的「从食材说明与膳食指南抽取关系，先落 `kg_candidate`」没有入口。

**9. Langfuse 没有节点级 span**

- 位置：`observability/tracing.py`，`api/chat.py`
- 需求：FR-06
- `record_span` 与 `record_score` 没有调用方。线上只有整次 `/chat` 一条 trace，带 `graph_hit`、`degraded`、`evidence_counts`。

**10. 设备维频控不计数，应用层 IP 可被伪造**

- 位置：`frontend/assets/app.js`，`api/deps.py` 的 `client_ip`，`deploy/nginx/nutri-graph-proxy.conf`
- 需求：FR-07
- 登录请求不传 `device_fp`，设备维频控在页面流量上不计数。`client_ip` 取 `X-Forwarded-For` 第一段；nginx 使用 `proxy_add_x_forwarded_for`，客户端可以伪造第一段，绕过应用层 IP 配额和验证码触发。边缘 `limit_req` 仍按 `$remote_addr`。手机号维度不受影响。systemd 已设 `--proxy-headers --forwarded-allow-ips=127.0.0.1`，应用代码仍自己切 header，没有用 uvicorn 解析后的 `request.client`。

### P2 容量与工程门禁

**11. 测试倒挂，没有静态检查门禁**

- 位置：`tests/`，仓库根
- 约 95 个用例集中在登录、频控、未收录、抓取、审核。意图、Cypher、分档、弃答、模型池、记忆读取没有单测。没有 `pyproject.toml`、ruff、mypy，也没有把文档里的 Ragas 基线做成门禁。

**12. 每次调用新建数据库连接**

- 位置：各模块里的 `psycopg.connect`
- 业务函数各自建连接，没有连接池。`deploy/systemd/nutri-graph.service` 固定 `--workers 1`，因为别名缓存和模型池状态在进程内。单进程能跑，连接次数随问答线性涨。

### 需求字面与实现

需求原文「同设备 10 天/次」与代码、README 的「10 次/天」字面不同。实现与 `security/ratelimit.py` 的模块说明一致，更像需求笔误。设备维当前仍因前端未传 `device_fp` 而不计数。

## 4. 测试分布

约 95 个 pytest 用例（含参数化展开），清点自 `tests/` 中的 `def test_` 与 `@pytest.mark.parametrize`。

| 落点 | 用例数 | 覆盖 |
|---|---:|---|
| 未收录登记 + USDA 抓取 | 48 | `tests/test_food_request.py`、`tests/test_fetch.py` |
| 登录、验证码、频控、匿名归并、管理鉴权 | 34 | `test_auth.py`、`test_captcha.py`、`test_sms_ratelimit.py`、`test_anon_merge.py`、`test_admin.py` |
| bundle 审核与幂等 MERGE | 13 | `tests/test_promote.py` |
| 问答路由 | 1 | `test_chat_routes_unknown_food_to_uncatalogued` |

直接断言问答路由的只有未收录这一条。意图选择、关系类型、弃答话术、证据分档没有对应用例。

## 5. 建议的改动顺序

1. 先为意图选择和 Cypher 选择写不依赖 LLM 的测试，锁住「菠菜豆腐」「西柚他汀」「上火吃什么」「无依据」四类问法各自命中的关系。再补 `INCOMPATIBLE_WITH` 模板，并让症状问法使用症状节点的 `canonical_id`。
2. 把向量降级和弃答拆开：图 0 命中且向量高于阈值时，上下文标明这是文档而不是图谱事实；低于阈值走现有弃答话术，且不要把低分原文交给模型。`verify_node` 在做成断言级检查之前，从关键路径拿掉。
3. 记忆二选一：把 `load_context` 放进生成提示词，或在需求里把短期记忆收成「只持久化本轮」。`LongTermMemory` 同理，接上召回或标成未启用。
4. `AliasStore` 改成进程级单例，问答链接在子串之后继续走模糊和向量，阈值读配置。`extract.py` 若短期不接抽取任务，就不要再把它算进 FR-02 的已落地范围。
5. IP 使用 nginx 写入的 `X-Real-IP`，或 uvicorn 在 `--proxy-headers` 下已经解析好的 `client.host`。页面若要设备维频控，登录请求带上 `device_fp`。

## 6. 已经值得保留的做法

- `app.py` 只装配路由，业务在 `api/`、`kg/`、`graph/`、`security/`。
- 主图写入收在 `kg.merge`。节点属性有白名单，边用 `relation_key` 做确定性 MERGE，带 `batch_id` 回滚。
- 未收录三出口、四维打分、单源封顶 C、provisional 过滤都有测试。
- 手机号 HMAC + Fernet + 掩码。refresh 只存哈希，旋转，重放吊销整族；登出吊销和重放吊销用 `replaced_by` 区分。
- 向量、记忆、登记失败时记日志并降级，避免 `/chat` 变成 500。模型池全不可用时返回「暂时答不了」，不编答案。
- 注释记录的是先前踩过的失败（中文 `pg_trgm` 全 0、Neo4j 不能存 map、占位符数量对不上），而不是复述函数名。
