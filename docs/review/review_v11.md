# plan_v11 评审（对照 needs_v11）· 第六轮

> 对照 [docs/needs/needs_v11.md](../needs/needs_v11.md)（第三节 3.3 是施工口径）与 [docs/plan/plan_v11.md](../plan/plan_v11.md)。
> 未改代码。日期：2026-11-02。
> 这是第六轮。第五轮的两句（`result` 同时写 `sessionId.value` 和 `localStorage`；空 `sid` 不放进 payload）已经写进 D3 和 D2 草图。
> §一 是结论。这一轮没有新的必须改项。

---

## 1. 结论

可以按 plan_v11 开工。

对照 needs 3.3：不改 `short_term.py`，会话按 `client_id` 列，流式复用 `chatbus` / `stream_model` / `spawn` / `bind` 并自建 `KbStreamer`，成功才 `remember`，非流式 `/kb/ask` 保留，最近问过这轮做。前五轮点过的落库、句末 `flush`、断开打标、放锁、`sid`、`sent_text`、取消后仍发 `error`/`done`、清图片顺序、ref 回写，正文都有，草图和正文一致。

第二节 KBQ-04 仍写「无流式」。与第三节冲突时以第三节为准，plan 已按流式写。

---

## 2. 写的时候照正文，不再单列

这些已经在第七节里，照着做即可：

- 没带租户时与 `ask()` 一样，在 `_session` 之前返回弃答 JSON，不开会话、不占信号量。`run_ask` 带上 `_session` 取出的 `history`。
- `result` 到达且 `session_id` 非空时，`sessionId.value` 和 `localStorage` 一起写。气泡只补 `hits`。
- 新对话在 `fresh()` 之后 `loadHistory()`。流式期间保持现有的 `busy`。
- 空标题显示「未命名对话」。
- Redis 密码只留在服务器 `.env`，不要再抄到别的文档。
