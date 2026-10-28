# 管理后台 v2 执行计划（食物录入 · 表格导入 · 中国数据源）

> 需求来源：`docs/needs/admin_needs_v2.md`（3 条）
> 评估方式：对照当前代码逐条核对，不按需求字面排期
> 状态：待评审。需求 2、3 可排期；需求 1 只立 TODO，不进入本期开发
> 说明：本文只定范围、接法与验收，不含代码实现

---

## 0. 结论摘要

| 需求 | 结论 | 优先级 | 核心理由 |
|---|---|---|---|
| 1. 从中国食材网站爬数据 | **方向对，但现在不能开发** | P2 / TODO | 网站未定、数据授权未明；现有抓取链路是 USDA 单源硬编码，不是「加个爬虫」 |
| 2. 支持手动录入食物 | **合理，但需求表述有歧义** | P0 | 现有「手工登记」只能登记名字并触发 USDA 抓取，不能填营养数据。真实缺口是「完整食物档案」 |
| 3. 按固定表格导入食物 | **合理，ROI 最高** | P0 | 可一次性补齐几十到几百个中式食材；也是需求 1 拿到授权数据后的低风险落地通道 |

**一句话方案**：需求 2 和需求 3 合并为同一个能力——「人工数据源」。两者都产出同一种 `kg_food_bundle`，复用现有「审核 → 候选 → 主图」链路，不新增任何写图入口。需求 1 本期只做数据源选型与接入预留，代码不动。

---

## 1. 现状核对（决定工作量的代码事实）

| # | 现状 | 位置 | 对本期的影响 |
|---|---|---|---|
| 1 | 「手工登记」只写 `kg_ingredient_request` 并排一条 `kg_fetch_task`，随后由 USDA 抓取 | `api/admin.py::create_food_request`、`kg/request.py::register` | 不能填营养成分、性味、别名；需求 2 目前只完成了「登记名字」这一半 |
| 2 | 抓取链路只有 USDA 一个源，`source_key='usda_fdc'` 写死 | `kg/fetch.py::_persist`、`kg/usda.py` | 需求 1 需要重构适配层，不是新增一个脚本 |
| 3 | 来源权威性白名单只有 `usda_fdc / china_fcd / web` | `kg/verify.py::SOURCE_AUTHORITY` | `manual`、`manual_import` 会命中「来源未在白名单内」的硬否决 |
| 4 | 单源得分封顶 0.88，低于自动通过线 0.9；`source_count=1` 也低于 2 | `kg/verify.py`、`config.py` | 所有抓取产物必然进人工审核，这是刻意设计，人工/导入也应沿用 |
| 5 | 主图唯一写入口是 `kg/merge`，节点属性有白名单 | `kg/merge.py::NODE_PROPS_WHITELIST` | 人工/导入不能直写 Neo4j，必须走候选 |
| 6 | `approve_bundle` 把边的 `evidence_type` 写死为 `'measured'` | `kg/promote.py::approve_bundle` | 人工录入的数据不是「实测」，直接复用会把证据语义写错 |
| 7 | `approve_bundle` 对已存在的 `canonical_id` 执行 `DO UPDATE SET props=…, aliases=…`，`merge` 再 `SET n += props` | `kg/promote.py`、`kg/merge.py::merge_nodes` | 手工/导入若命中既有别名，会**覆盖**原节点的分类、摘要、别名 |
| 8 | 图谱营养素词表有 20 项，但 `promote` 只能解析 6 项 | `data/crosswalk/nutrient_map.csv` vs `kg/nutrients.py::resolve` | 导入钙、铁、锌、维 C 等会被**静默跳过**，只留一条 info 日志 |
| 9 | `kg_food_bundle` 的唯一约束是 `UNIQUE(request_id)` | `migrations/006_food_bundle.sql` | 人工/导入 bundle 的 `request_id` 为 NULL，唯一约束对其不生效，需要额外去重 |
| 10 | 后台只有「未收录」「补录审核」等页面，无录入表单、无导入预览 | `admin-web/src/views/` | 需求 2、3 都要补前端 |
| 11 | 后端没有文件上传依赖 | `requirements.txt` | 需要 `python-multipart`；XLSX 还需要 `openpyxl` |
| 12 | `merge_pending(None)` 会合并**所有**历史 approved 候选，并为所有 merged 节点重写别名/重试 Milvus | `kg/promote.py::merge_pending` | 批量导入几百条时不能直接调 `None`，需要按批次范围合并 |

---

## 2. 需求合理性逐条评估

### 2.1 需求 1：从中国食材网站爬数据

**方向正确，但当前不具备开工条件。**

正确的部分：

- 单一来源永远无法自动通过（0.88 < 0.9），也不符合「多源交叉验证」的设计。补第二来源是既定路线。
- USDA 对中式食材、药食同源、性味几乎无覆盖，中国来源确实是当前最大数据缺口。
- 现有代码已经为第二来源留了名字：`verify.SOURCE_AUTHORITY` 里有 `china_fcd`。

三个硬问题：

1. **目标网站未定，字段未确认。**
   「中国食材网」可能是食材百科/菜谱站，未必提供「每 100g 能量、蛋白质、脂肪、碳水、矿物质」这类成分数据。如果只有名称、别名、做法，那它只能补实体与别名，不能补营养。**在确认站点和字段之前写爬虫，做出来的东西很可能用不上。**

2. **数据授权与合规是硬门槛。**
   `kg/usda.py` 文件头已经写明：中国食物成分表查询平台没有公开 API，只能抓 HTML，且数据不可商用。本项目按 `docs/合规手册-养生了么上架.md` 要上架应用市场，数据版权/商用授权必须先解决。爬站还要确认 `robots.txt`、ToS、访问频率。

3. **工程上不是「加个爬虫」。**
   `kg/fetch.py` 的检索、详情、字段解析都绑死 USDA（`fdc_query`、`fdcId`、`parse_nutrients`）；`verify()` 按 `source_key` 打分；`promote()` 又把 `evidence_type` 写死。接第二个源需要：
   - 抽出 source adapter 接口；
   - 定义同一食材两源数值冲突时的合并策略；
   - 维护 `source_count` 与来源溯源；
   - 抓取结果先落 staging 表，匹配后再生成 bundle，不能把网页结果直接写成 bundle。

**本期处理**：只做「选型 + 授权确认 + 字段映射」，代码只保留已经顺带做掉的 `evidence_type` 透传（见第 4 节）。真正的抓取实现等第 7 节的准入清单全部满足后再排期。

**补充建议：需求 3 是需求 1 的降险替代路径。** 如果能拿到中国食物成分表的授权 Excel/数据文件，直接走需求 3 的导入通道即可，不需要爬站，也不会有 robots/ToS 风险。这是当前性价比最高的做法。

---

### 2.2 需求 2：支持手动录入食物

**合理，但需求字面有歧义，需要先确认口径。**

- 如果指「手工登记一个食物名字」：**已经上线**（`Requests.vue` 的「手工登记」→ `POST /admin/kg/requests`）。再做成一样的东西就是重复劳动。
- 如果指「手工填写完整食物档案并进入主图」：这是真实缺口，也是 `admin_needs_v1.md` 第 3 节明确排除、留到后续的能力。**本计划按这一口径设计。**

为什么必须现在做：需求 1 是 TODO，在第二来源接入之前，中式食材只能靠人工补。没有完整录入能力，「未收录食物」的闭环仍然是断的——用户登记了，抓取抓不到，管理员也无处可填。

必须遵守的既有约束：

- 不能直写 Neo4j，只能产出 `kg_food_bundle` → `kg_node_candidate` / `kg_candidate` → `kg/merge`。
- 人工数据不是「实测」数据。`evidence_type` 不能写 `measured`，除非来源引用本身就是成分表；`provisional=true`、`evidence_level=C`，检索时降级引用并标注。
- 录入前必须复用既有的别名/近义防线（`register()` 里的 `exact_lookup` + `fuzzy_lookup(0.62)`），否则「黑大豆」「乌豆」会被建成第二个黑豆。这正是 9.1 花力气防的事。
- 命中既有节点时，不能覆盖原节点的分类、摘要、别名（见 1.7 的风险）。

**结论**：合理，P0。前提是先确认「完整档案」口径。

---

### 2.3 需求 3：按固定表格导入食物

**合理，ROI 最高；但「固定表格」必须先定义契约，否则导入等于制造脏数据。**

表格导入看起来简单，实际有六个必须先定的点：

1. **列契约**：哪些必填、单位、口径（统一每 100g）、枚举值（分类、性味）。
2. **营养素词表**：图谱里已有 20 项（`nutrient_map.csv`），但 `promote` 只认 6 项。不改这一层，导入钙、铁、锌、维 C 会静默丢失。
3. **重复与冲突**：同一 `food_key`、同一别名、同一 `canonical_name` 分别怎么处理；文件内重复怎么处理。
4. **部分成功语义**：一行错不能整批失败；必须给行级错误与可下载的错误报告。
5. **模板版本**：`template_version` 必须记录，否则以后改列无法解释历史数据。
6. **执行方式**：解析同步、审核/合并分步；几百行不能要求管理员逐条点。

**结论**：合理，P0。与需求 2 同批上线，共用同一条写入链路。

---

### 2.4 三条需求的共同点（最重要的判断）

三条需求本质上是同一件事：**给主图增加一个新的数据来源**。

- 需求 2 = 单条人工数据源；
- 需求 3 = 批量人工数据源；
- 需求 1 = 外部抓取数据源。

主图只能有一个写入口。正确做法不是为三条需求各写一套逻辑，而是抽出一个统一的「人工数据源」出口：**无论单条录入还是表格导入，都产出同构的 `kg_food_bundle`，后面完全复用 `approve → candidate → merge`。** 需求 1 将来接进来，也走同一个 bundle 出口，只是前面的采集方式不同。

---

## 3. 关键设计决策

| 编号 | 决策 | 理由 |
|---|---|---|
| D1 | 人工录入/导入**不写** `kg_ingredient_request`，也不建 `kg_fetch_task` | 它们不是「待抓取请求」，不应消耗抓取预算；但合并后要反向关闭同名的未结请求（见 5.6） |
| D2 | 人工/导入一律 `pending_review`，不自动通过 | 与现有「单源不自动通过」一致；导入量再大也要过一道人工闸 |
| D3 | 数值型营养素**必须填来源引用**，没有引用不允许提交 | 健康类产品，无出处的数字比没有数字更危险 |
| D4 | 人工/导入数据默认 `provisional=true`、`evidence_level=C`；提供两条以上独立来源时可标为已交叉验证 | 不破坏现有证据分层；同时给高质量录入一条「转正」通道 |
| D5 | 复用 `kg/promote.py` 与 `kg/merge.py`，不新增写图入口 | 保证「这条数据是谁、什么时候、从哪来的」可追溯、可回滚 |
| D6 | 导入模板 v1 同时支持 CSV（UTF-8 BOM）与 XLSX | CSV 零依赖；XLSX 是管理员的实际习惯 |
| D7 | 需求 1 本期只做选型与接口预留，不实现抓取 | 目标与授权未定，提前写代码会返工 |

---

## 4. 数据模型与迁移

新增迁移 `migrations/012_manual_import.sql`。

### 4.1 给既有表补字段

```sql
-- 数据从哪条通道进来：抓取 / 手工 / 表格导入
ALTER TABLE kg_food_bundle
    ADD COLUMN IF NOT EXISTS entry_channel TEXT NOT NULL DEFAULT 'crawl';
-- crawl | manual | import

-- 这批数据的证据类型。抓取仍为 measured；手工/导入按来源选择。
ALTER TABLE kg_food_bundle
    ADD COLUMN IF NOT EXISTS evidence_type TEXT NOT NULL DEFAULT 'measured';
-- measured | traditional | anecdotal | guideline

-- 导入任务归属，便于整批查看与回滚
ALTER TABLE kg_food_bundle
    ADD COLUMN IF NOT EXISTS import_job_id BIGINT;

-- 节点候选的写入方式：新建 / 仅增量补全既有节点
ALTER TABLE kg_node_candidate
    ADD COLUMN IF NOT EXISTS merge_mode TEXT NOT NULL DEFAULT 'create';
-- create | augment
```

### 4.2 新增导入任务表

```sql
CREATE TABLE IF NOT EXISTS kg_import_job (
    id               BIGSERIAL PRIMARY KEY,
    kind             TEXT NOT NULL DEFAULT 'table',   -- table | manual
    filename         TEXT NOT NULL DEFAULT '',
    file_sha256      TEXT NOT NULL DEFAULT '',
    template_version TEXT NOT NULL DEFAULT 'v1',
    status           TEXT NOT NULL DEFAULT 'preview_ready',
                     -- preview_ready | committed | approved | failed | expired
    row_total        INT  NOT NULL DEFAULT 0,
    row_ok           INT  NOT NULL DEFAULT 0,
    row_failed       INT  NOT NULL DEFAULT 0,
    created_by       TEXT NOT NULL DEFAULT '',
    note             TEXT NOT NULL DEFAULT '',
    created_at       TIMESTAMPTZ NOT NULL DEFAULT now(),
    finished_at      TIMESTAMPTZ
);

CREATE TABLE IF NOT EXISTS kg_import_row (
    id         BIGSERIAL PRIMARY KEY,
    job_id     BIGINT NOT NULL REFERENCES kg_import_job (id) ON DELETE CASCADE,
    row_no     INT    NOT NULL,
    raw_name   TEXT   NOT NULL DEFAULT '',
    food_key   TEXT   NOT NULL DEFAULT '',
    action     TEXT   NOT NULL DEFAULT '',
               -- new | merge_existing | alias_candidate | invalid
               -- duplicate_in_file | duplicate_existing_pending
    bundle_id  BIGINT,
    message    TEXT   NOT NULL DEFAULT '',
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    UNIQUE (job_id, row_no)
);
CREATE INDEX IF NOT EXISTS idx_import_row_job ON kg_import_row (job_id, row_no);
```

### 4.3 手工/导入 bundle 的去重

`kg_food_bundle` 现有 `UNIQUE(request_id)` 对 NULL 不生效，人工/导入会堆出重复 bundle。分两层处理：

**第一层：数据库只约束人工/导入之间的重复。**

```sql
-- 上线前先查重，历史上若已有重叠会建索引失败
SELECT food_key, count(*) FROM kg_food_bundle
WHERE entry_channel IN ('manual', 'import')
  AND status IN ('draft', 'pending_review', 'approved')
GROUP BY food_key HAVING count(*) > 1;

CREATE UNIQUE INDEX IF NOT EXISTS uq_open_bundle_manual_food
    ON kg_food_bundle (food_key)
    WHERE entry_channel IN ('manual', 'import')
      AND status IN ('draft', 'pending_review', 'approved');
```

索引**只覆盖 manual/import**，不覆盖 crawl。原因：抓取侧 `kg/fetch.py::_persist` 会先删旧 bundle 再插入同一 `food_key`，如果索引把 crawl 也算进去，一个在审的人工 bundle 会让抓取任务直接插库失败。不能为了去重把抓取链路弄崩。

**第二层：应用层跨通道拦一道。**

新建手工/导入 bundle 前，先查**任意通道**的未结 bundle：

```sql
SELECT id, entry_channel, status FROM kg_food_bundle
WHERE food_key = %s AND status IN ('draft', 'pending_review', 'approved')
ORDER BY id DESC LIMIT 1;
```

- 命中 `manual/import` → 409 `open_bundle_exists`，返回 `bundle_id`，引导去编辑/合并；
- 命中 `crawl` → 409 `pending_crawl_bundle`，提示「该食材已在抓取审核队列中」，避免抓取与人工同时写同一食材；
- 管理员确认要接管时，可先驳回抓取 bundle，再建人工 bundle。

这样数据库约束与抓取链路互不干扰，跨通道的冲突由接口给出明确提示。

### 4.4 营养素词表（代码层，不需要迁移）

`kg/nutrients.py` 目前只解析 6 项，而 `data/crosswalk/nutrient_map.csv` 已有 20 项。本期把它补全为统一映射：

- `EXISTING` / `EXTRA_NUTRIENTS` 覆盖 `NUT_00001`–`NUT_00020` 中所有可挂边的营养素（维 C、维 A、B1、叶酸、维 K、钙、铁、锌、钾、镁、膳食纤维、蛋白质、脂肪、碳水、胡萝卜素、番茄红素、花青素、ω-3、嘌呤、咖啡因）；
- `能量` 仍用 `NUT_00021`（kcal），`钠` 仍用 `NUT_00022`；
- `resolve()` 查不到时不再静默返回 `None`，而是返回「未知营养素」结果，由导入校验层直接报行级错误。

这一步不做，需求 3 的模板里一半列都会白填。

---

## 5. 需求 2 详细设计：手工录入

### 5.1 状态机

```
draft ──submit──> pending_review ──approve──> approved ──merge──> merged
                        └──reject──> rejected
```

- 新建时可选「存草稿」或「直接提交审核」。
- 驳回必须写理由（沿用 `kg/promote.py::reject_bundle` 的既有约束）。
- 通过仍走现有 `POST /admin/kg/bundles/{id}/approve`，不新增写图逻辑。

### 5.2 接口

全部挂在现有 `/admin` router 下，沿用 `require_admin`。

| 方法 | 路径 | 作用 |
|---|---|---|
| POST | `/admin/foods/manual` | 新建手工食物档案。参数 `submit=true` 时直接进 `pending_review` |
| GET | `/admin/foods/manual/{bundle_id}` | 查看档案（复用 bundle 详情，额外返回 conflicts） |
| GET | `/admin/foods/manual` | 手工/导入 bundle 列表，支持状态、关键词、`entry_channel`、分页 |
| POST | `/admin/foods/manual/{bundle_id}/submit` | 草稿提交审核 |
| POST | `/admin/kg/bundles/{bundle_id}/approve` | 复用现有通过接口 |
| POST | `/admin/kg/bundles/{bundle_id}/reject` | 复用现有驳回接口 |

请求体示意：

```json
{
  "name": "藜麦",
  "aliases": ["南美藜", "昆诺阿藜"],
  "category": "谷物",
  "nature": "平",
  "english_name": "quinoa",
  "scientific_name": "Chenopodium quinoa",
  "nutrients": [
    {"name": "能量", "value": 368, "unit": "kcal"},
    {"name": "蛋白质", "value": 14.1, "unit": "g"},
    {"name": "脂肪", "value": 6.1, "unit": "g"},
    {"name": "碳水化合物", "value": 64.2, "unit": "g"},
    {"name": "膳食纤维", "value": 7.0, "unit": "g"},
    {"name": "钙", "value": 47, "unit": "mg"}
  ],
  "sources": [
    {
      "type": "database",
      "title": "中国食物成分表（第6版）",
      "publisher": "北京大学医学出版社",
      "year": 2018,
      "locator": "第 1 册，第 xx 页",
      "url": ""
    }
  ],
  "submit": true
}
```

成功返回：`{ "bundle_id": 123, "status": "pending_review", "action": "created", "warnings": [] }`

### 5.3 校验规则

| 字段 | 规则 | 失败处理 |
|---|---|---|
| `name` | 1–40 字；复用 `is_plausible_food()`；不得是纯符号 | 422，指出具体原因 |
| `aliases` | ≤10 个，每个 1–40 字；`;`/`；` 分隔；去重 | 422 |
| `category` | 固定枚举：蔬菜/水果/谷物/豆类/坚果/肉类/禽类/水产/蛋类/奶类/菌藻/油脂/调味/其他 | 422 |
| `nature` | 可选；枚举：寒/凉/平/温/热 | 422 |
| `nutrients` | ≥4 项；名称必须在词表内；单位必须与该营养素一致；数值 ≥0 | 422，列出不合法项 |
| 数值上限 | 能量 ≤900 kcal/100g；钠 ≤40000 mg；其余按营养素设合理上限 | 422，提示疑似单位错误 |
| `sources` | 至少 1 条，`title` 或 `url` 必填；最多 3 条 | 422 |
| 重复 | `name`/别名命中既有 `dict_alias` → 409；近义相似度 ≥0.62 → 409 并给候选 | 409，附 `canonical_id` / 候选列表 |

补充：

- 0 必须显式写 0，空值不等于 0。
- 不做单位自动换算（mg/g、kcal/kJ）。单位错就报错，避免「120 斤存成 120 公斤」这类事故。
- `source_count` 按 `sources` 中不同 `publisher` 计数，最大 2；≥2 时允许管理员勾选「已交叉核对」，此时 `provisional=false`。

### 5.4 冲突处理（录入前）

复用 `register()` 已有的两道判断，但抽出为只读的 `precheck(name)`：

1. `exact_lookup` 命中 `Ingredient` → 返回 409 `already_catalogued`，附 `canonical_id`，提示「已有该食材，请走补充营养/别名流程」。
2. `fuzzy_lookup` 命中 ≥0.62 → 返回 409 `alias_candidate`，附候选，提示「可能是某某的新叫法」。
3. 管理员确认「不是同一种」时，可传 `force_new=true`，但必须填写理由，写入审计日志。

`precheck` 只读，不产生 `dict_alias_candidate` 副作用；别名候选的写入仍由 `register()` 负责。

### 5.5 主图写入保护

必须修 `kg/promote.py::approve_bundle` 的两个行为：

1. **`evidence_type` 从 bundle 读取**，不再写死 `measured`。
2. **区分 `create` 与 `augment`**：
   - 新节点（`_resolve_canonical_id` 返回 `created`）→ `merge_mode='create'`；
   - 命中既有别名 → `merge_mode='augment'`，只补边、补别名并集、补空属性；**不覆盖** `canonical_name`、`category`、`summary`。
   - `kg/merge.py::merge_nodes` 对 `augment` 使用 `coalesce` 与别名并集，不用 `SET n += row.props` 整体覆盖。

这一条同时修掉一个潜在缺陷：当前 `SET n += row.props` 会把既有节点的 `aliases` 整个替换掉，补录一个新别名可能丢三个旧别名。

### 5.6 与既有「未收录请求」闭环

手工录入/导入合并成功后，按 `food_key` 反查：

```sql
UPDATE kg_ingredient_request SET status = 'approved' WHERE food_key = %s;
UPDATE kg_fetch_task SET status = 'rejected' WHERE food_key = %s
  AND status IN ('queued', 'running');
```

理由：用户登记过的食材，管理员手工补齐后不应再被爬虫抓一遍，也不应在「未收录」列表里继续挂着。

### 5.7 证据语义

| 数据 | evidence_type | evidence_level | provisional | 说明 |
|---|---|---|---|---|
| 抓取（USDA/未来中国源） | `measured` | C | true | 现状不变 |
| 手工录入，引用成分表 | `measured` | C | true | 数值来自实测表，但单一来源未交叉验证 |
| 手工录入，引用典籍的性味 | `traditional` | C | true | 性味只作为节点属性，不挂边 |
| 手工录入，无明确出处 | **不允许提交** | — | — | 无据不录 |
| 两条独立来源且管理员确认 | `measured` | B | false | 才允许进 Milvus 向量兜底 |

注意：`provisional=true` 的节点不会进 Milvus（`promote._index_into_milvus` 会跳过），因此**只能通过别名精确命中，不能被向量兜底召回**。这是既有设计，不是本期 bug。若希望人工录入的食材尽快可被模糊召回，就走「两条独立来源 → 转正」通道。

### 5.8 前端

新增 `admin-web/src/views/ManualEntry.vue`：

- 分区：基本信息 / 别名 / 营养素 / 来源引用；
- 营养素区按模板固定行，单位只读；
- 提交前展示冲突提示（已收录 / 疑似别名）；
- 提交成功后跳转 `BundleDetail`，管理员可在同一页通过或驳回；
- 菜单增加「食物录入」。

---

## 6. 需求 3 详细设计：固定表格导入

### 6.1 模板 v1（宽表，一行一个食物）

| 列名 | 必填 | 类型/单位 | 说明 |
|---|---|---|---|
| `名称` | 是 | 文本 1–40 字 | 规范名，同时作为 `food_key` 的来源 |
| `别名` | 否 | 文本，`;` 分隔，≤10 个 | 写入 `dict_alias` |
| `分类` | 是 | 枚举 | 同 5.3 |
| `性味` | 否 | 枚举 | 寒/凉/平/温/热 |
| `英文名` | 否 | 文本 | `english_name` |
| `学名` | 否 | 文本 | `scientific_name` |
| `能量(kcal)` | 是 | 数值/100g | 对应 NUT_00021 |
| `蛋白质(g)` | 是 | 数值/100g | NUT_00012 |
| `脂肪(g)` | 是 | 数值/100g | NUT_00013 |
| `碳水化合物(g)` | 是 | 数值/100g | NUT_00014 |
| `膳食纤维(g)` | 否 | 数值/100g | NUT_00011 |
| `钠(mg)` | 否 | 数值/100g | NUT_00022 |
| `钙(mg)` | 否 | 数值/100g | NUT_00006 |
| `铁(mg)` | 否 | 数值/100g | NUT_00007 |
| `锌(mg)` | 否 | 数值/100g | NUT_00008 |
| `钾(mg)` | 否 | 数值/100g | NUT_00009 |
| `镁(mg)` | 否 | 数值/100g | NUT_00010 |
| `维生素C(mg)` | 否 | 数值/100g | NUT_00001 |
| `维生素A(μgRAE)` | 否 | 数值/100g | NUT_00002 |
| `胡萝卜素(μg)` | 否 | 数值/100g | NUT_00015 |
| `嘌呤(mg)` | 否 | 数值/100g | NUT_00019 |
| `来源` | 是 | 文本 | 书名/版本/页码或 URL；整行共用 |
| `备注` | 否 | 文本 ≤200 字 | 不进图，仅存 bundle |

必填的四个营养素（能量、蛋白质、脂肪、碳水）同时满足「≥4 项」的完整性要求，避免与 `verify.MIN_NUTRIENTS` 冲突。

模板同时提供 CSV（UTF-8 BOM）和 XLSX 两个版本，列名与顺序完全一致。

### 6.2 行级动作

预览阶段为每行判定一个动作，管理员可逐行改（仅限 `new` 与 `merge_existing` 之间）：

| action | 含义 | 提交时的处理 |
|---|---|---|
| `new` | 新食材 | 创建 bundle → `pending_review` |
| `merge_existing` | 命中既有规范名/别名 | 创建 `merge_mode='augment'` 的 bundle，只补边与别名 |
| `alias_candidate` | 近义但不确定 | 默认跳过，提示走别名确认流程 |
| `invalid` | 校验失败 | 跳过，记录原因 |
| `duplicate_in_file` | 文件内重复 | 只保留首行，其余跳过 |
| `duplicate_existing_pending` | 已有未结 bundle | 默认跳过，可勾选「合并进已有 bundle」 |

### 6.3 接口

| 方法 | 路径 | 作用 |
|---|---|---|
| GET | `/admin/imports/template?format=csv\|xlsx` | 下载模板 |
| POST | `/admin/imports/preview` | `multipart/form-data` 上传，解析并返回 `job_id` + 汇总 + 行结果 |
| GET | `/admin/imports/{job_id}` | 任务详情 + 行结果（分页） |
| POST | `/admin/imports/{job_id}/commit` | 为 `new` / `merge_existing` 行创建 bundle |
| POST | `/admin/imports/{job_id}/approve` | 批量通过该任务的全部 bundle 并合并 |
| GET | `/admin/imports` | 导入历史 |

### 6.4 流程

```
上传 → 解析校验（不写 bundle）→ 预览（统计 + 逐行动作）
     → 管理员确认 → commit（创建 bundle，状态 pending_review）
     → approve（批量通过 + 一次合并）
```

关键点：

- 预览阶段**不写 bundle**，只写 `kg_import_job` / `kg_import_row`，避免「点错一次生成几百条脏草稿」。
- 预览结果保留 30 分钟，过期置 `expired`。
- commit 与 approve 分开：先让管理员看清有多少行、错在哪，再决定是否入库。
- approve 必须整批成功或整批停止（只跳过状态异常的 bundle），返回失败清单。

### 6.5 批量通过必须做「范围合并」

现有 `merge_pending(bundle_id=None)` 会合并**全部历史** approved 候选，并为所有 merged 节点重写别名、重试 Milvus。导入 500 条时直接调它会非常慢，还会把无关的历史节点重新处理一遍。

需要扩展为按批次合并：

```python
def merge_pending(bundle_id=None, batch_ids: list[str] | None = None) -> dict:
    ...
```

- `approve` 阶段对每个 bundle 调 `approve_bundle(merge_now=False)`，收集 `batch_id = f"bundle:{id}"`；
- 最后调一次 `merge_pending(batch_ids=[...])`；
- 别名同步与 Milvus 索引只处理这批节点。

### 6.6 限制

| 项 | 上限 | 理由 |
|---|---|---|
| 单文件行数 | 500 | 一次审核可承受；更多拆文件 |
| 单文件大小 | 2 MB | 防止误传大文件 |
| 单元格长度 | 400 字 | 防止异常内容 |
| 导入频率 | 每管理员每天 10 次 | 防止把后台当批量接口刷 |
| 预览有效期 | 30 分钟 | 避免陈旧预览被提交 |

编码：CSV 只接受 UTF-8 / UTF-8 BOM；GBK 文件给出明确错误并提示另存为 UTF-8。XLSX 用 `openpyxl` 读取第一个工作表。

### 6.7 前端

新增两个页面：

- `Import.vue`：模板下载、拖拽上传、预览表格（动作色标）、汇总统计、提交、批量通过；
- `ImportJobs.vue`：历史任务、行级错误、错误报告下载。

菜单增加「批量导入」。`Layout.vue` 同步。

---

## 7. 需求 1 TODO：中国数据源

### 7.1 准入清单（全部满足才排期）

| # | 条件 | 判定方式 |
|---|---|---|
| 1 | 有每 100g 成分字段，至少覆盖能量、蛋白质、脂肪、碳水、钠、钙、铁、锌、维 C 中的 8 项 | 抽样 20 条核对 |
| 2 | 有稳定的食材标识与规范中文名，能对应到别名 | 抽样比对现有 `ingredient_canonical.csv` |
| 3 | 授权可商用/可再分发，或可购买授权 | 书面授权/许可条款 |
| 4 | 访问方式允许自动化：官方 API 或授权数据文件优先；网页抓取需 robots 允许、ToS 允许、有频率上限 | 站点条款 + 实测 |
| 5 | 常见中式食材覆盖 ≥500 条 | 数据统计 |
| 6 | 有版本号与更新周期 | 数据说明 |

### 7.2 工程预留（本期只做前两项）

1. `evidence_type` 透传（本计划第 4 节已含），让新来源不必再改 `promote`。
2. `SOURCE_AUTHORITY` 增加来源键（新增来源时再加，避免提前放空值）。
3. 抽 `kg/sources/` 适配接口：`search(query) -> candidates`、`get_detail(id) -> normalized food + nutrients + provenance`；`kg/fetch.py` 按 `source_key` 调度，不再直接写 `fdcId`。
4. 抓取结果先落 `kg_source_staging`，匹配 `food_key` 后再生成 bundle；网页结果不直接进 bundle。
5. 多源合并策略：同一食材两源数值差异超过阈值时告警，不静默取其一；`source_count` 由实际命中的来源数决定。
6. 调度：`scripts/run_fetch.py` 目前是手工 CLI，正式抓取需要 systemd timer/cron + 失败告警。

### 7.3 推荐路径

优先「授权数据文件 → 需求 3 导入通道」，把爬站作为最后手段。这样需求 1 的大部分价值可以在不碰版权风险的前提下先拿到。

### 7.4 官方获取渠道（2026-10-28 实测）

**结论：没有官方「全量数据一键下载」入口。** 只有三条路——在线免费查询（量少、不可导出）、付费书（唯一完整载体）、数据中心申请（需审批、商用收费）。

| 渠道 | 地址 | 内容 | 能否批量拿到 | 商用 |
|---|---|---|---|---|
| 食物营养成分查询平台（营养所） | https://nlc.chinanutri.cn/fq/ | 1300+ 条常见食物在线查阅，页脚「营养所 版权所有」 | 否，无导出、无 API | 未授权 |
| 旧查询域名 | https://fq.chinafcd.org | 已失效（实测 502） | — | — |
| 公共卫生科学数据中心 | https://www.phsciencedata.cn/Share/ky_sjml.jsp?id=577e0301-ab65-432a-9bb7-a8342302e589 | 基于 2002 版，1506 条 × 31 项营养成分 | 仅「食物编码表.xlsx」可下，实体数据需申请 | 需申请 |
| 国家人口健康科学数据中心 | https://www.ncmi.cn/phda/dataDetails.do?id=7ebf7c15-fce4-36b8-b3e5-71555e1da935 | 中国食物成分数据库 V1.0（同为 2002 版） | 「实体数据共享方式：暂不共享」 | 「公益免费、商业收费」；未经书面许可不得修改及销售 |
| 第 6 版工具书（唯一完整数据） | 中国营养学会推荐页 https://www.cnsoc.org/latesachie/311911205.html | 第一册植物性 1110+ 条；第二册动物性 3600+ 条（1005 条为一般营养成分） | 需购纸书，无官方免费电子版 | 版权作品，需出版社授权 |
| 版权方：营养所 / 数据中心 | http://www.chinanutri.cn/（010-67791292）；数据中心联系人 王松旺 010-58900412、wangsw@chinacdc.cn | — | — | 商用授权从这里谈 |

> GitHub 上的 OCR 版（如 `china-food-composition-data`）与网盘流传的 PDF 均属未授权转载，上架审核时是明确法律风险，不可作为数据来源。

**落地建议（按成本排序）**：

1. **走数据中心正式申请**（ncmi / phsciencedata），说明商用场景，付费拿授权数据文件，直接进需求 3 导入通道。数据虽基于 2002 版，但对常见中式食材够用，合规链完整。
2. **采购第 6 版两册 + 向出版社/营养所申请数据使用授权**，再人工整理成导入模板。数据最新，整理成本高。
3. **不要**规模化抄录在线查询平台（无授权声明），最多作为人工核对参考。

---

## 8. 执行顺序

### Phase 0：地基（必须先做）

| # | 任务 | 产出 | 估时 |
|---|---|---|---|
| 0.1 | 补全 `kg/nutrients.py` 词表（20 项）与未知项报错 | 代码 + 单测 | 0.5d |
| 0.2 | `kg_food_bundle` 增加 `entry_channel`、`evidence_type`、`import_job_id`；`kg_node_candidate` 增加 `merge_mode` | 迁移 012 | 0.5d |
| 0.3 | `promote.approve_bundle` 读取 `evidence_type`；区分 create/augment；`merge_nodes` 支持增量合并 | 代码 + 单测 | 1d |
| 0.4 | `merge_pending` 支持 `batch_ids` 范围合并 | 代码 + 单测 | 0.5d |

### Phase 1：需求 2 手工录入

| # | 任务 | 产出 | 估时 |
|---|---|---|---|
| 1.1 | 抽出 `precheck(name)`（只读，复用 exact/fuzzy） | `kg/request.py` 重构 | 0.5d |
| 1.2 | `kg/manual.py`：创建 bundle + items + 证据语义 | 代码 | 0.5d |
| 1.3 | 5 个接口 + 校验 + 冲突响应 | `api/admin.py` 或 `api/foods.py` | 1d |
| 1.4 | `ManualEntry.vue` + 菜单 + 冲突提示 | 前端 | 0.5d |
| 1.5 | 合并后关闭同名未结请求/任务 | 代码 | 0.5d |

### Phase 2：需求 3 表格导入

| # | 任务 | 产出 | 估时 |
|---|---|---|---|
| 2.1 | 模板 v1（CSV/XLSX）+ 下载接口 | 文件 + 接口 | 0.5d |
| 2.2 | `kg/importing.py`：解析、校验、行级动作、预览 | 代码 | 1d |
| 2.3 | preview / commit / approve / 历史接口 | 后端 | 1d |
| 2.4 | `Import.vue` + `ImportJobs.vue` + 菜单 | 前端 | 1d |
| 2.5 | 批量 approve + 范围合并联调 | 代码 | 0.5d |

### Phase 3：需求 1（只做选型，不写抓取）

| # | 任务 | 产出 | 估时 |
|---|---|---|---|
| 3.1 | 数据源调研与准入清单核对 | 调研记录 | 1d |
| 3.2 | 授权/许可确认 | 结论 | 依赖外部 |
| 3.3 | 字段映射草案（若源确定） | 文档 | 0.5d |

### Phase 4：部署与验收

| # | 任务 | 说明 |
|---|---|---|
| 4.1 | 远程执行迁移 012 | `python -m scripts.migrate` |
| 4.2 | 重启 `nutri-graph` | `systemctl restart nutri-graph` |
| 4.3 | 构建后台并部署 | `admin-web` build → `/var/www/nutri_graph/console/` |
| 4.4 | 按第 10 节逐条验收 | — |

**P0 合计约 10.5 人日**（Phase 0–2 + 部署），不含需求 1 实现。

---

## 9. 测试计划

遵循既有铁律：**不在本机跑需要连库/连图的测试**。改完代码同步到远程 `/opt/code/nutri_graph`，用 `/opt/envs/nutri/bin/python` 在 `nutri_graph_test` 库跑。

新增测试文件：

| 文件 | 覆盖点 |
|---|---|
| `tests/test_nutrient_vocab.py` | 20 项词表全部可解析；未知营养素返回明确错误；`energy_kj` 仍跳过 |
| `tests/test_manual_entry.py` | 新建草稿/直接提交；必填校验；分类/性味枚举；营养素不足；未知营养素；来源必填；已收录 409；疑似别名 409；非管理员 403 |
| `tests/test_promote_augment.py` | 命中既有别名走 `augment`；不覆盖 `canonical_name/category/summary`；别名并集；新节点仍走 `create` |
| `tests/test_import.py` | CSV/XLSX 解析；行级错误；文件内重复；`merge_existing`；commit 生成 bundle 数正确；重复提交幂等 |
| `tests/test_import_approve.py` | 批量通过；范围合并只影响本批次；失败清单；重复 approve 不重复建边 |
| `tests/test_manual_close_request.py` | 手工合并后同名 request 变 `approved`、fetch task 变 `rejected` |

必须保留的回归：

- `tests/test_food_request.py`：用户侧登记行为不变；
- `tests/test_promote.py`：既有抓取 bundle 的通过/驳回不变；
- `tests/test_admin.py`：越权与既有接口不变。

重点断言：

1. 手工/导入 bundle 的 `request_id` 为 NULL，且**没有**新增 `kg_fetch_task`；
2. `kg_candidate.evidence_type` 不再是 `measured`（当来源为典籍/无实测时）；
3. `kg_node_candidate.props->>'provisional'` 为 `true`；
4. 已存在节点的 `category`、`summary`、`aliases` 在 `augment` 后保持不变（别名只增不减）；
5. 同一条边重复 approve 不会产生第二条（`relation_key` 幂等）。

---

## 10. 验收标准

### 需求 2

- [ ] 管理员能录入「藜麦」：名称、2 个别名、分类、性味、6 项营养素、1 条来源引用。
- [ ] 提交后出现在待审列表，状态 `pending_review`；详情页能看到全部条目与来源。
- [ ] 通过后主图出现 `Ingredient` 节点与 6 条 `CONTAINS_NUTRIENT` 边；`evidence_type` 为 `measured`，`provisional=true`。
- [ ] 问答里用「藜麦」能通过别名精确命中并回答；引用中标注「单一来源/人工录入」。
- [ ] 录入「黑大豆」（已存在别名为「黑豆」）时被 409 拦下，并提示既有 `canonical_id`；不产生第二个节点。
- [ ] 营养素只填 3 项、或填了词表外的「硒」时，提交被拒绝并指出具体项。
- [ ] 来源引用为空时不能提交。
- [ ] 非管理员调用任一录入接口返回 403。
- [ ] 录入后没有新增 `kg_fetch_task`；同名未结 request 被置为 `approved`。

### 需求 3

- [ ] 能下载 CSV/XLSX 模板，列名与第 6.1 节一致。
- [ ] 上传 20 行文件，预览显示 `new 15 / merge_existing 2 / invalid 2 / duplicate_in_file 1`，逐行有原因。
- [ ] 预览阶段数据库中没有新增 bundle。
- [ ] commit 后生成 17 个 bundle，全部 `pending_review`。
- [ ] 批量通过后 17 个食材进入主图；invalid 行被跳过并出现在错误报告里。
- [ ] 再次导入同一文件，不产生重复节点或重复边。
- [ ] 对既有「菠菜」执行 `merge_existing`，只新增缺失的营养素边与别名，原分类、摘要、别名不被覆盖。
- [ ] 500 行文件在 2 分钟内完成预览；超过 500 行被拒绝并提示拆分。
- [ ] 批量通过的合并只处理本任务批次，不重扫历史节点。

### 需求 1（本期只验文档）

- [ ] 第 7.1 节准入清单已逐项核对，结论与证据留档。
- [ ] 未确定来源前，代码中没有新增抓取实现。

---

## 11. 风险与对策

| 风险 | 影响 | 对策 |
|---|---|---|
| 人工录错数值 | 错误营养数据进入主线回答 | 来源引用必填；默认 `pending_review`；`provisional` 降级引用；`batch_id` 可追溯 |
| 别名冲突 | 同一食材多个节点 | 录入前 `precheck`；导入预览标 `alias_candidate`；合并时 `sync_aliases` 不抢别名 |
| 覆盖既有节点 | 原有分类/摘要/别名被改坏 | `merge_mode='augment'` + `coalesce` + 别名并集；单测锁定 |
| 营养素词表不全 | 导入数据静默丢失 | Phase 0.1 先补全；导入校验对未知项直接报错 |
| 导入中途失败 | 半批数据入库 | 先 commit 后 approve 分两步；行级状态可查；失败清单可见 |
| 批量合并过慢 | 审核超时 | `merge_pending(batch_ids=...)` 范围合并；避免 `None` 全量 |
| 模板变更 | 历史文件无法解释 | `template_version` 入库；模板列只增不改，废弃列标 deprecated |
| 数据授权 | 上架合规风险 | 需求 1 准入清单；优先授权数据文件，不爬站 |
| 管理员误操作 | 批量脏数据 | 预览不写库；commit 需确认；每日导入次数限制；驳回必须写理由 |

---

## 12. 明确不做

- 不做「在后台点按钮启动抓取」；抓取仍是离线 CLI/定时任务。
- 不做「编辑既有食材的分类/摘要/别名」；本期只做新增与增量补营养。要改既有节点，另立「食物编辑」需求。
- 不做自动通过（人工/导入一律过审）。
- 不做多角色权限（仍只有 `admin`；editor 角色以后再说）。
- 不做菜品、食谱、图片、人群/症状/禁忌的录入。
- 不做单位自动换算。
- 不做需求 1 的实际抓取实现。
- 不改问答页 `frontend/`。

---

## 13. 待确认问题

1. **需求 2 的口径**：是「完整食物档案（含营养素）」还是仅「登记名字」？后者已上线。本计划按前者设计。
2. **导入是否允许补充既有食材**：本计划允许 `merge_existing`（只补边/别名，不改节点属性）。若不允许，导入只能新增。
3. **分类与性味枚举**：是否采用第 5.3/6.1 节的取值？现有种子只有「蔬菜/凉」，需要业务确认完整词表。
4. **人工数据是否立即对用户可见**：默认按 `INCLUDE_PROVISIONAL=true` 可见并标注；若要严格模式先隐藏，需要另加开关。
5. **中国数据源**：是否已有授权数据/具体站点？没有的话，需求 1 只能停留在 TODO。
6. **录入权限**：是否仅 `admin`？如果要给运营人员开放但不想给审核权，需要新增角色。

---

## 14. 附：模板示例（CSV 表头 + 一行）

```csv
名称,别名,分类,性味,英文名,学名,能量(kcal),蛋白质(g),脂肪(g),碳水化合物(g),膳食纤维(g),钠(mg),钙(mg),铁(mg),锌(mg),钾(mg),镁(mg),维生素C(mg),维生素A(μgRAE),胡萝卜素(μg),嘌呤(mg),来源,备注
藜麦,南美藜;昆诺阿藜,谷物,平,quinoa,Chenopodium quinoa,368,14.1,6.1,64.2,7.0,5,47,4.6,3.1,563,197,0,1,1,0,中国食物成分表（第6版）第1册,示例行，导入前请删除
```

---

## 附：与 v1 的关系

`admin_needs_v1.md` 第 3 节把「手工填写营养成分并写入图谱」列为不做，理由是「主图只有补录审核一条写入口，页面直接改图会绕开它」。本计划没有推翻这个判断，而是补上了当时缺的那条路：**手工/导入也走 bundle → 候选 → `kg/merge`，写入口仍然只有一个。** 这也是本计划把「地基」放在需求 2/3 之前的原因。

---

## 15. 落地记录：公开导入并入审核链路（2026-10-28）

面试演示要**不登录、所有人都能上传 Excel**。第一版（当天上午）为了「别让公开入口污染审核队列」，
把结果写进了独立的 `demo_food`；下午改成**同一条链路**，因为整站都是演示环境，
两套数据库只会让「导入的数据查不到」，反而更糟。

### 15.1 第一版为什么被推翻

| 项 | 独立 `demo_food`（旧） | 并入 `kg_food_bundle`（现在） |
|---|---|---|
| 导入后能否被问答检索 | 不能 | 能（审核通过后走 `kg.merge`） |
| 是否有证据/批次/可回滚 | 没有 | 有（bundle → candidate → batch_id） |
| 重复导入 | 按 `name_norm` 覆盖一张表 | 覆盖「未结」的那份 bundle，已 merged 的不动 |
| 代码分支 | 导入页一套、审核链一套 | 一套 |

原来那句「公开入口会刷满待审队列」的顾虑，用 1MB / 500 行 / 每 IP 20 次/天 三道闸 + 人工审核解决，
不需要为此再造一张表。

### 15.2 现在的链路

```
上传 → importer 解析校验 → kg_import_job / kg_import_row（批次留档）
     → 提交：kg/import_bundle.py 写 kg_food_bundle(status=pending_review, source_key=excel_import)
     → 后台 /console「补录审核」通过 → kg_node_candidate / kg_candidate
     → kg.merge → Neo4j → 问答 / 图谱探索 / 食材对比 全部可查
```

### 15.3 已交付

| 文件 | 作用 |
|---|---|
| `migrations/012_demo_import.sql` | 初版批次表与 `demo_food`（013 里改名/删除） |
| `migrations/013_import_bundle.sql` | `demo_import_*` → `kg_import_*`；删 `demo_food`；bundle 增 `aliases` / `evidence_type`；导入来源的部分唯一索引 |
| `kg/importer.py` | 列契约（模板/解析/校验/前端字段说明共用）、xlsx/csv 解析、行级校验 |
| `kg/import_bundle.py` | 行 → bundle（同食材覆盖未结 bundle，`pg_advisory_xact_lock` 串行化并发上传） |
| `kg/nutrients.py` | 词表从 6 项补到 20 项（`nutrient_map.csv`）+ 能量/钠，导入的钙铁锌维C 不再被静默丢弃 |
| `kg/promote.py` | 别名取 bundle.aliases；`evidence_type` 透传；`provenance` 用 `source_key`；节点候选改为「合并」而非「整体覆盖」 |
| `api/food_import.py` | `/kg/import/meta｜preview｜{id}/commit｜foods｜jobs｜stats` |
| `scripts/make_import_template.py` | 由契约生成 xlsx（3 sheet、分类/性味下拉）+ CSV |
| `frontend/import.html` + `assets/import.{css,js}` | 导入页：模板 → 拖拽 → 行级预览 → 提交审核 → 卡片带审核状态 + 审核台入口 |
| `tests/test_importer.py` | 41 条；含「每一列都必须能映射到图谱营养素 id」的守门用例 |

### 15.4 三个必须记住的实现细节

1. **营养素词表是静默失败点**。`resolve()` 查不到就 `log.info` 跳过，不报错。
   导入 15 项只进 4 项，页面一切正常 —— 所以加了 `test_every_nutrient_column_resolves_to_its_graph_id`。
2. **别名命中既有节点时，不能让新数据改它的名字和分类**。
   `dict_alias` 里「豆腐/南豆腐/北豆腐/老豆腐」都指向 `ING_00003`；
   导入一行「北豆腐」若直接写 `canonical_name`，这个节点的显示名就被一个别名劫持了。
   `_STICKY_PROPS`（summary / provenance / canonical_name / category）只在旧值为空时才写入。
3. **边的数值不在 `amount` 属性上**。`kg.merge` 只写 `claim_text`，
   `kg/subgraph.py::_resolve_amount` 从「藜麦每 100g 含钙 47.0 mg」里解析。
   所以 `claim_text` 的格式是接口契约的一部分，改它要连带改解析。

---

## 16. 落地记录：三条需求的收口（2026-10-28 下午）

需求 3（表格导入）上线后剩下三件事，按顺序做完。

### 16.1 食物库改服务端搜索

原来 `/assets/import.js` 一次性 `GET /foods?limit=500` 再在前端过滤，
超过 500 条的部分**静默消失**（接口本来支持 `q` / `category` / `offset`，前端没用）。

现在：每页 60 条，关键词 300ms 防抖打服务端，分类 chip 也是服务端筛，
底部「加载更多（还有 N 条）」按 `count > 已渲染` 决定显隐；请求带序号，
过期响应直接丢弃（否则快速输入时旧结果会盖掉新结果）。

### 16.2 爬虫数据并入同一条链路（需求 1）

`fcd_crawler` 抓的 1356 条（nlc.chinanutri.cn/fq）不再只躺在仓库外的 JSON 里：

```
data/external/fcd_foods.json
  → scripts/import_fcd.py（映射 + 复用 importer 校验）
  → kg_food_bundle(source_key=fcd_crawler, pending_review)
  → approve_bundle → kg.merge → Neo4j
```

**实测结果**：可导入 1339 / 1356，入队 1330 份（9 条重名折叠），
`kg.merge` 合并 1327 个**新节点** + 14325 条营养边，12 条挂到既有节点上；
全程走审核链路，问答与 `/kg/subgraph` 都查得到（如「小麦粉(标准粉)」10 条营养边，
能量已按 ÷4.184 转成 353.3 kcal）。

三个关键取舍：

1. **只映射图谱有节点的营养素**（13 项 + 能量）。原表 32 项里，
   磷/硒/铜/锰/碘/胆固醇/脂肪酸/B 族维生素在图谱没有节点，硬塞会被
   `nutrients.resolve()` 静默丢掉 —— 不如显式不导，见 `import_fcd.NUTRIENT_MAP`。
   > **2026-10-28 已推翻**：这批营养素已经补进词表（见 §17），
   > 现在原表 31 项里除水分/灰分/食部/脂肪酸总量外全部导入。
2. **`relax_required=True`**。原表空白 = 未测定，不是漏填：
   含酒精饮料 47 条无蛋白/脂肪/碳水、食用油 19 条无碳水，按人填表的严格模式
   会被整批拒掉。放松只作用于这一条路径，「至少 4 项营养素」的闸门仍然生效，
   17 条太空的行照样进不来。
3. **阈值按真实数据修正**：铁 200→400（苔菜干 283.7）、钾 5000→15000（竹荪干 11882）、
   维生素A 5000→30000（羊肝 20972）。这三个不是脏数据，是干制品和内脏的真实含量 ——
   说明上限原本定得太窄。

脚本幂等：已 `merged` 的 food_key 直接跳过（重跑不会把营养边翻倍）；
`--reconcile` 补齐「候选已进图但 bundle 还挂在 approved」的那几份
（同一 food_key 在抓取包里出现两次时会发生）。

### 16.3 手工录入（需求 2）

`POST /kg/import/manual` + 页面上「或者，直接填一条」卡片。

- 表单控件由 `/kg/import/meta` 的 `columns` / `nutrients` 生成，**前端不抄字段表**；
- 服务端把表单摊成「一行表格」交给同一个 `importer.validate_row`，
  校验失败返回 422 + 逐字段错误，前端据此标红对应输入框；
- 成功写 `kg_food_bundle(source_key=manual_entry, pending_review)`，与表格导入同一个队列；
- 同名再提交是覆盖同一个 bundle（`action=updated`），不堆第二条待审；
- 限流独立：每 IP 每天 40 条（`FOOD_MANUAL_DAILY_LIMIT_PER_IP`），
  不与「上传一个 500 行文件」共用计数器。

### 16.4 本轮新增/改动文件

| 文件 | 变化 |
|---|---|
| `migrations/014_import_sources.sql` | 批次表加 `source_key`；唯一索引改为 `(food_key, source_key)` |
| `migrations/015_manual_entry_source.sql` | 索引与批次来源扩到 `manual_entry` |
| `scripts/import_fcd.py` | 抓取包 → 审核队列；`--check / --limit / --approve / --reconcile` |
| `kg/importer.py` | `validate_row(relax_required=)`；铁/钾/维生素A 上限修正 |
| `kg/import_bundle.py` | 三个来源常量 + `stage_bundles(source_key=)`；看板/统计取来源并集 |
| `api/food_import.py` | `POST /manual`；限流按来源分账；`/jobs` 带 `source_key` |
| `frontend/import.html` + `assets/import.{css,js}` | 服务端搜索 + 加载更多；手工录入表单 |
| `tests/test_import_fcd.py` / `tests/test_import_manual.py` | 11 + 7 条 |

全量测试 205 条通过（`nutri_graph_test` 独立库）。

> 顺带修掉一个**一直存在**的用例级隐患：`tests/test_admin.py::_food_name()` 的字母表里有「丁」，
> 而 `kg.request` 的切分后缀表也含「丁」（肉丁），于是「测丙丙丁」会被切成「测丙丙」，
> `food_key != 输入名`，用例约 27% 概率随机失败（3 个名字各 10%）。已把「丁」从字母表移除。

---

## 17. 营养素词表扩容：13 项 → 28 项（2026-10-28）

### 17.1 为什么做

导入模板只有 15 个营养列，图谱只认 22 个营养素节点，而中国食物成分表这份数据源
有 31 项。中间那段不是「暂时不需要」，而是**填了也进不去**：

| 环节 | 扩容前 | 扩容后 |
|---|---|---|
| Excel 模板列（`kg/importer.COLUMNS`） | 15 | **28** |
| 图谱营养素节点 | 22（NUT_00001~00022） | **34**（+NUT_00023~00034） |
| 爬虫映射（`import_fcd.NUTRIENT_MAP`） | 13 项 + 能量 | **26 项 + 能量** |

失败模式有两种，都不报错：Excel 里加了模板没有的列 → 解析时静默忽略；
模板有列但图谱没节点 → `nutrients.resolve()` 返回 None，审核时静默跳过。

### 17.2 新增的 12 个节点

| id | 名称 | 单位 | 抓取包覆盖率 | 上限的依据 |
|---|---|---|---|---|
| NUT_00023 | 磷 | mg | 92.3% | 酵母(干) 1893 |
| NUT_00024 | 维生素B2（核黄素） | mg | 88.3% | 干菇 6.9 |
| NUT_00025 | 烟酸（尼克酸/B3） | mg | 81.0% | 干菌菇 67.66 |
| NUT_00026 | 硒 | μg | 75.7% | 牛肝菌干 758.8 |
| NUT_00027 | 铜 | mg | 85.9% | 酵母(鲜) 20.12 |
| NUT_00028 | 胆固醇 | mg | 28.3% | 蛋黄粉 2850 |
| NUT_00029 | 锰 | mg | 81.6% | 河蚌 59.61 |
| NUT_00030 | 维生素E（α-TE） | mg | 10.8% | 芝麻油 67.43 |
| NUT_00031 | 碘 | μg | 14.0% | 裙带菜干 15878 |
| NUT_00032 | 饱和脂肪酸 | **%E** | 37.6% | 上限 110 |
| NUT_00033 | 单不饱和脂肪酸 | **%E** | 37.0% | 上限 110 |
| NUT_00034 | 多不饱和脂肪酸 | **%E** | 36.1% | 上限 110 |

维生素B1（NUT_00003）原本就在图里，只是模板没列 —— 这次补上列与爬虫映射（硫胺素）。

**%E 不是克**。原表这三种脂肪酸给的是「占总能量百分比」，导进图谱时单位就写 `%E`，
不做任何换算（饱和脂肪酸 100.8% 的海苔是真实值，按克判上限会被当成脏数据）。
`parse_number` 只额外接受一个等价写法：`24` / `24%` / `24%E` 都认。

### 17.3 边上的 amount / unit（迁移 016）

营养数值以前只存在于 `claim_text` 里（「黑豆每 100g 含蛋白质 36.49 g」），
前端表格和「谁含铁更高」只能靠正则回捞。%E 正好是这个做法的反例：
正则只认得到 `%`，`E` 丢掉。于是：

- `migrations/016_candidate_amount.sql`：`kg_candidate` 加 `amount` / `unit`；
- `kg/promote.py` 写候选时一并落库（`_as_float`，转不动就是 NULL＝没有数值，不是 0）；
- `kg/merge.py` 写关系属性 `r.amount` / `r.unit`，**None 时不写**
  （Cypher 里 `SET r.x = null` 等于删属性，会把别的来源的数值抹掉）；
- 读取端 `kg/subgraph._resolve_amount` 本来就优先用 amount，老边继续走文本回捞。

### 17.4 顺带修掉两个「静默丢数据」的坑

**坑一：479 个食材根本没进图。**

扩完词表跑补边脚本，发现「图里已有 9090 条营养边、要补 15218 条」——
其中 479 条是**能量**，不可能。查下去：

> PG 里 1331 个食材候选状态全是 `merged`，Neo4j 里只有 888 个节点，
> **479 个食材（含种子里就有的「猪肝」）根本没进图**，且 `ING_01585` 起是连续区间。

两个原因叠加：

1. `kg/merge.py` 的 `s.run(...)` 没有 `.consume()`。neo4j 驱动的 auto-commit 写事务
   要到结果被读完才提交，不消费就往下走，会话结束时的 RESET 会把没提交的写回滚掉，
   而 PG 侧照样把候选标成 `merged`。
2. **`rollback()` 用 `provenance CONTAINS $batch` 匹配节点**：
   `CONTAINS 'bundle:12'` 会同时命中 `bundle:120`、`bundle:1200`… ——
   回滚一份 bundle:12，把后面一百多份一起删了。测试用例里
   `rollback("bundle:12")`（测试库的 bid 很小）正好打到生产图。

修法：每次 `s.run()` 加 `.consume()`；`rollback` 改成
`provenance = $bid OR provenance STARTS WITH "$bid "`（精确 + 带空格前缀）；
`scripts/backfill_nutrients.py --repair-nodes` 按候选表把缺的节点原样补回；
补边脚本加守门：待补边的食材若在图中不存在，直接拒绝执行（否则 Cypher 只「匹配不到」，
脚本会打印成功但一条没写）。

**坑二：跑测试会改生产图。**

PG 可以指到 `nutri_graph_test`，Neo4j 社区版却**只有一个库**，测试和生产共用同一张图。
`tests/test_promote.py` 会造节点、跑审核、再清理 —— 清理写松一点就是删生产数据
（上面 120 个节点就是这么没的）。

修法：`test_promote` 打 `graph` 标记，`conftest._graph_guard` 默认跳过，
要跑必须显式 `ALLOW_GRAPH_TESTS=1`；清理也不再走 `rollback(batch)`，
而是按本用例的 `canonical_id` + `provenance ∈ 本批次` 双重限定精确删除。

实测：修完后跑全量测试，图里节点/边数**一条没变**（24364 / 1368）。

### 17.5 补边脚本

`scripts/backfill_nutrients.py`：已 merged 的抓取包不会因为词表扩容而自动长出边
（幂等按 food_key 跳过、当时的值也没落库），只能回抓取包重读一遍。

- 走 `kg_candidate → kg.merge` 单一写入口，整批一个 `batch_id`，`--rollback` 可整批撤；
- 只补图上**没有**的边，已有数值一律不覆盖（那是审过的数据）；
- 按 `(食材, 营养素)` 去重：同一 food_key 出现两次时不会把边补成两倍。

### 17.6 实测结果（线上）

- `scripts/sync_nutrients.py`：新建 12 个营养素节点、36 条别名（硫胺素/核黄素/尼克酸/α-生育酚…）；
- `--repair-nodes`：补回 479 个食材节点；后来又被测试误删的 120 个节点同样补回。
  图里 `:Ingredient` 最终 **1368** 个（888 → 1367 → 1368，最后一个见 §17.8），
  与候选表的 1331 份 bundle 对得上
  （其余 37 个是种子里没有营养数据的食材，外加 4 份 Excel 导入的）；
- 补边：**15218 条**营养边入图，主图营养边 9151 → **24358** 条
  （+ 黑豆复原 6 条 = **24364**），其中 16416 条带 amount/unit；
  磷 1241、B2 1183、铜 1150、B1 1117、锰 1095、烟酸 1087、硒 1017、
  三种脂肪酸 500/492/479、胆固醇 384、碘 184、维生素E 140；
- `/kg/subgraph?food=猪肝` 返回 **23** 条边（原来 13 条 + 新增 10 条；维E、碘这两项
  该食材没有数据，所以不是 12），含「磷 310 mg」「维生素B2 2.08 mg」「胆固醇 288 mg」
  「饱和脂肪酸 43.2 %E」「单不饱和 27.3 %E」「多不饱和 26 %E」，amount/unit 直接来自边属性；
- `/kg/import/meta` → 28 项营养素、36 列；模板重生成（`填写说明` 顺带改正了
  「只进演示库」这句早已过时的说明）；
- 全量测试 **206 passed / 13 skipped**（新增：CSV↔resolver 一致性、模板列 id 唯一、
  %E 单位容错、表头同义词、示例行对齐、爬虫 31 项不静默丢、补边差集；
  13 skipped 是写图的 test_promote，见 §17.4 坑二）。

### 17.7 已知取舍

- **水分/灰分/食部不导**：不是营养素；食部不进图谱，避免「每 100g 可食部」被当营养素查。
- **脂肪酸总量（fatty_acid_total）不导**：就是 sfa+mufa+pufa 之和，导了等于同一件事说两遍。
- **叶酸/维生素K/番茄红素/花青素/ω-3/咖啡因仍只在种子里**：这几项中国成分表没有，
  没有批量数据源，暂不开放模板列（要开就是一行 `Column(...)`，图谱节点已存在）。
- **维生素E 按 α-生育酚计（mg）**，不是 IU；IU 换算涉及构型，不做。

### 17.8 `--restore`：按审计记录单个复原食材（2026-10-28）

补边脚本只能补**边**，前提是食材节点还在。上面那次测试误删里有一个漏网的：
`dict_alias` 仍指向 `ING_01075`（黑豆 / soybeans），但它的
`kg_node_candidate` / `kg_food_bundle` 行在某次测试清库时被连带删掉，
节点没了、只有 `kg_candidate` 里 6 条审计边还活着 ——
于是 `/kg/subgraph?food=黑豆` 返回 404 `not_found`。
`--repair-nodes` 救不了它（源头那行已经不存在），补边脚本的守门会直接拒绝。

所以补一个 `python -m scripts.backfill_nutrients --restore <canonical_id>`：
从**存活的 `kg_candidate` 审计行**反推节点该长什么样（名称取 claim_text 前缀、
别名取 `dict_alias`、属性从边汇总），重写一条 `kg_node_candidate`
（`provenance = "<批次> restore:<cid>"`），把这批边状态翻回 `approved`，
再照常走 `kg.merge` 写图 —— 依然不绕过单一写入口。

- `--dry-run` 只打印「别名 xx，待复原 N 条边（批次 bundle:133）」，不写库；
- 复原后 `ING_01075` 回到图中，`/kg/subgraph?food=黑豆` 200、7 节点 6 边，
  amount/unit 都在（蛋白 36.49 g、纤维 9.3 g、能量 446 kcal…）。
- 这是**抢救工具不是常规流程**：正常数据不该只有边没有节点。
  真要用它，说明上游某次删库漏了 `dict_alias`，复原后该顺手查一遍还有没有别的孤儿别名。
