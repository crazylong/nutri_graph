# 《管理后台 v1 执行计划》评审意见（三轮·终审）

> 评审对象：`docs/needs/admin_needs_v1.md`（已按二轮意见修订版）
> 评审依据：对照当前代码逐条核对接法是否成立。二轮唯一遗留点（改 `register()` 跳过个人限流）本轮已闭环。

## 总体结论

**可行，批准实施。** 三轮修订把二轮唯一遗留的「改 `kg/request.py`」正式写入执行顺序（第 86 行），并把限额口径、`skip_user_limit` 参数设计、全局限流的错误转译、以及测试覆盖点都落到了文档。新增的技术断言经代码核对全部成立。无遗留问题。

---

## 三轮核对结果（新增断言全部成立）

- **`register()` 现签名与限流逻辑描述准确**：`kg/request.py:184` 签名为 `register(raw, who="anonymous", source="chat", near_misses=None)`；`request.py:263-270` 个人限流只按 `who` 计数、`source` 不参与——文档第 71 行的复述与代码逐字一致。
- **`skip_user_limit` 参数设计成立**：在 `request.py:264` 的个人限流判断处短路即可，全局限流（`request.py:270`）保持不变。默认 `False` 保证问答/图谱探索两条既有路径行为不变，向后兼容。
- **全局限流返回 `code="global_daily_limit"` 准确**：`request.py:272-275` 命中全局限流时 `RegisterResult(code="global_daily_limit", action="rate_limited")`，且**不抛 HTTP 异常**（`register()` 对异常是兜底返回，见 `request.py:328-333`）。文档第 74 行「`register()` 返回 code、自己不抛 HTTP 错误，管理员接口要把结果转成明确错误」的描述与实现完全吻合——这个转译要求是正确且必要的，因为 `register()` 返回 `rate_limited` 时并非异常，接口层若直接透传 `RegisterResult` 会让人误以为「登记成功」。
- **驳回只改状态、写 `reject_reason` 成立**：`005_food_request.sql:25` 有 `reject_reason TEXT NOT NULL DEFAULT ''`，理由为空落空串与列默认值一致。
- **手工登记幂等语义成立**：`register()` 的 UPSERT `ON CONFLICT (food_key)` 已累加 `hit_count`、刷新 `raw_mention`/`last_requested_at`；`source`/`status` 不在 UPDATE 子句里、保持原值；`rejected` 分支（`request.py:301-309`）返回 `duplicated` 且不重新排队——文档第 76 行的描述与实现逐条吻合。
- **`kg_fetch_task` 排队逻辑成立**：`request.py:295-299` 仅当 `status == "queued"` 时插任务、`ON CONFLICT (food_key) DO NOTHING`，与文档「同一 food_key 已有任务则不再插」一致。
- **改库设 admin 后刷新即生效成立**（复核）：`require_admin`/`/auth/me` 均经 `load_user` 每次查库，不读 access token 的 role 快照；`rotate_refresh` 刷新时也重读 role（`tokens.py:152-156`）。

---

## 执行顺序与测试覆盖（已充分）

第 86 行把「先改 `register()` 加 `skip_user_limit` → 再补 5 个接口 → 手工登记传 `skip_user_limit=True`」排对了顺序，并明确了测试覆盖点：

- 列表、越权（非管理员 403）、驳回写 `reject_reason`、手工登记（新行 `source=admin` + 有抓取任务、重复登记只增 `hit_count`、个人限流不拦截、全站上限返回错误）；
- **「用户侧登记的既有测试保持通过」** —— 这是关键且被正确强调的一点：`skip_user_limit` 默认 `False` 必须保证 `tests/test_food_request.py` 现有用例（含 `monkeypatch` 个人限流的 204/214 行）不回归。

---

## 结论

**批准实施。** 三轮下来文档已做到「范围克制、接法与代码逐条对齐、测试与验收口径可落地」，无遗留问题。按第 7 节执行顺序开工即可，无需再改需求。
