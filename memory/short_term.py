"""
memory/short_term.py —— 短期记忆（PostgreSQL）

短期 = 最近 N 轮 message + session 滚动摘要。
"""
from __future__ import annotations

import json
import logging
import threading
import uuid
from typing import Any

import psycopg
from psycopg.types.json import Jsonb

import config

import db

log = logging.getLogger("memory.short_term")

# 侧栏标题只取首问的前若干字，不再为标题打模型。
SESSION_TITLE_CHARS = 40
SESSION_LIST_LIMIT = 50
RECENT_QUESTION_LIMIT = 20


def _json_card(card: dict | None) -> dict:
    """丢掉不能进 JSON 的值。卡片翻译失败不能把整轮对话一起丢掉。"""
    try:
        return json.loads(json.dumps(card or {}, ensure_ascii=False, default=str))
    except (TypeError, ValueError):
        return {}


_SUMMARY_PROMPT = """\
你是食疗问答的记忆整理助手。把「已有摘要」和「新增对话」合并成一份摘要。

固定四段，不要自由发挥：
1) 用户约束：过敏 / 忌口 / 症状 / 人群（孕妇、老人…）
2) 已确认未收录的食材（避免重复登记）
3) 已给出的建议要点（≤5 条，每条 ≤20 字）
4) 用户问过的问题清单

只输出合并后的摘要正文，不要小标题以外的解释。
"""


# 模型失败后跳过接下来若干次调用，避免每轮都再打一次挂掉的模型。
# (剩余跳过次数, 下一档间隔)。成功则清掉。间隔 1、2、4、8，到 8 封顶。
_backoff_guard = threading.Lock()
_backoff: dict[str, tuple[int, int]] = {}

_PENDING_MARK = "更早对话尚未摘要"


def _compress_should_skip(session_id: str) -> bool:
    with _backoff_guard:
        state = _backoff.get(session_id)
        if not state or state[0] <= 0:
            return False
        _backoff[session_id] = (state[0] - 1, state[1])
        return True


def _compress_failed(session_id: str) -> None:
    with _backoff_guard:
        prev = _backoff.get(session_id)
        delay = 1 if not prev else min(prev[1] * 2, 8)
        _backoff[session_id] = (delay, delay)


def _compress_ok(session_id: str) -> None:
    with _backoff_guard:
        _backoff.pop(session_id, None)


def _cap_pending(text: str) -> str:
    """只截进提示词的那一段。不改库里的消息，也不写进 session.summary。"""
    limit = config.MEMORY_PENDING_MAX_CHARS
    if limit <= 0 or len(text) <= limit:
        return text
    return f"{_PENDING_MARK}\n{text[-limit:]}"


def _summarize(prev: str, inc: str) -> str:
    """合并式摘要。没配密钥时退化成拼接截断。模型异常必须抛出去，不能当成摘要成功。"""
    if not config.DASHSCOPE_API_KEY:
        body = "\n".join(inc.splitlines()[-20:])
        return ((prev + "\n" + body) if prev else body)[:4000]
    from llm import invoke_model

    try:
        resp = invoke_model(
            f"{_SUMMARY_PROMPT}\n已有摘要：\n{prev or '（无）'}\n\n新增对话：\n{inc}",
            enable_thinking=False,
        )
    except Exception:
        log.warning("摘要模型调用失败", exc_info=True)
        raise
    text = getattr(resp, "content", resp) or ""
    return str(text).strip()[:4000]


class ShortTermMemory:
    def __init__(self, dsn: str = None):
        # 保留 dsn 参数只为兼容旧签名。连接一律走 db.connect()（池按 config.PG_DSN 建），
        # 传别的 DSN 不会生效 —— 需要连别的库就别复用这个类。
        self.dsn = dsn or config.PG_DSN

    def create_session(self, user_id: str) -> str:
        """总是新开一会话。不复用 ended_at 为空的旧会话。"""
        sid = str(uuid.uuid4())
        with db.connect() as conn:
            conn.execute(
                "INSERT INTO session (id, user_id) VALUES (%s, %s)",
                (sid, user_id),
            )
        return sid

    def owns(self, user_id: str, session_id: str) -> bool:
        """session_id 必须是本人的会话。非法 id 当不存在。"""
        try:
            sid = str(uuid.UUID(str(session_id)))
        except (ValueError, AttributeError, TypeError):
            return False
        with db.connect() as conn:
            row = conn.execute(
                "SELECT 1 FROM session WHERE id = %s AND user_id = %s",
                (sid, user_id),
            ).fetchone()
        return row is not None

    def load_context(self, session_id: str, n: int = None) -> str:
        """上下文分三段拼，缺一段就会整段丢上下文。

        ① 最近 N 轮 → 原文
        ② 水位线**已盖住**的更早轮次 → 用 session.summary
        ③ 水位线**还没盖住**的更早轮次 → **仍用原文**

        第三段不能少：摘要是后台线程写的，第 4 轮刚落库时它还没跑完，
        只拼「摘要 + 最近 3 轮」的话第 1 轮此刻两头都不在 ——
        数据还在库里，却从提示词里凭空消失。
        """
        retain = n or config.MEMORY_RETAIN_TURNS
        with db.connect() as conn:
            cur = conn.cursor()
            cur.execute(
                "SELECT COALESCE(summary, ''), COALESCE(summary_until_msg_id, 0) "
                "FROM session WHERE id = %s",
                (session_id,),
            )
            row = cur.fetchone()
            summary = row[0] if row else ""
            waterline = int(row[1]) if row else 0

            # 一轮的最大 turn_no，用来算保留窗口的下界
            cur.execute(
                "SELECT COALESCE(MAX(turn_no), 0) FROM message WHERE session_id = %s",
                (session_id,),
            )
            max_turn = int(cur.fetchone()[0])
            cutoff = max(max_turn - retain, 0)

            cur.execute(
                "SELECT turn_no, role, content FROM message WHERE session_id = %s "
                "ORDER BY turn_no ASC, id ASC",
                (session_id,),
            )
            rows = cur.fetchall()

        recent: list[str] = []
        older_raw: list[str] = []
        for turn_no, role, content in rows:
            line = f"{role}: {content}"
            if turn_no > cutoff:
                recent.append(line)
            else:
                older_raw.append(line)

        parts: list[str] = []
        if summary:
            parts.append(f"[较早对话摘要]\n{summary}")
        # 水位线还没盖住的更早轮次：仍然给原文
        if older_raw and waterline <= 0:
            parts.append("[更早对话原文]\n" + _cap_pending("\n".join(older_raw)))
        elif older_raw:
            # 部分覆盖：只把水位线之后的原文补上，避免和摘要重复
            with db.connect() as conn:
                cur = conn.cursor()
                cur.execute(
                    "SELECT role, content FROM message WHERE session_id = %s "
                    "  AND turn_no <= %s AND id > %s ORDER BY turn_no ASC, id ASC",
                    (session_id, cutoff, waterline),
                )
                pending = [f"{r[0]}: {r[1]}" for r in cur.fetchall()]
            if pending:
                parts.append("[更早对话原文]\n" + _cap_pending("\n".join(pending)))
        if recent:
            parts.append("[最近对话]\n" + "\n".join(recent))
        return "\n\n".join(parts)

    def save_round(
        self,
        session_id: str,
        question: str,
        answer: str,
        card: dict | None = None,
    ) -> None:
        title = " ".join((question or "").split())[:SESSION_TITLE_CHARS]
        with db.connect() as conn:
            # 轮号在同一事务里取，并对 session 行加锁：
            # 同一会话并发两个请求时，不会拿到同一个 turn_no。
            # "一轮要么整轮落库、要么不落" —— 否则读取侧会在轮中间切开。
            cur = conn.cursor()
            cur.execute("SELECT id FROM session WHERE id = %s FOR UPDATE", (session_id,))
            if cur.fetchone() is None:
                return
            cur.execute(
                "SELECT COALESCE(MAX(turn_no), 0) FROM message WHERE session_id = %s",
                (session_id,),
            )
            turn_no = int(cur.fetchone()[0]) + 1

            cur.execute(
                "INSERT INTO message (session_id, role, content, turn_no, kind) "
                "VALUES (%s, 'user', %s, %s, 'human')",
                (session_id, question, turn_no),
            )
            cur.execute(
                "INSERT INTO message (session_id, role, content, meta, turn_no, kind) "
                "VALUES (%s, 'assistant', %s, %s, %s, 'ai')",
                (session_id, answer, Jsonb(_json_card(card)), turn_no),
            )
            # 标题只用首条用户问题。之后的轮次不再改。
            if title:
                conn.execute(
                    "UPDATE session SET title = %s "
                    "WHERE id = %s AND COALESCE(title, '') = ''",
                    (title, session_id),
                )
        return turn_no

    def list_sessions(self, user_id: str, limit: int = SESSION_LIST_LIMIT) -> list[dict[str, Any]]:
        """按最后一条消息时间倒序。没有消息时用 started_at。"""
        with db.connect() as conn:
            rows = conn.execute(
                """
                SELECT s.id::text,
                       COALESCE(s.title, ''),
                       s.started_at,
                       COALESCE(MAX(m.created_at), s.started_at) AS last_at,
                       COUNT(m.id),
                       COALESCE(s.summary, ''),
                       COALESCE(MAX(m.turn_no), 0)
                FROM session s
                LEFT JOIN message m ON m.session_id = s.id
                WHERE s.user_id = %s
                GROUP BY s.id
                ORDER BY COALESCE(MAX(m.created_at), s.started_at) DESC
                LIMIT %s
                """,
                (user_id, limit),
            ).fetchall()
        return [
            {
                "id": row[0],
                "title": row[1],
                "started_at": row[2],
                "last_at": row[3],
                "message_count": row[4],
                # 接口先给，前端侧栏展不展示是另一个决定（摘要里含过敏/症状）
                "summary": row[5],
                "round_count": int(row[6] or 0),
            }
            for row in rows
        ]

    def list_messages(
        self, user_id: str, session_id: str, q: str = ""
    ) -> list[dict[str, Any]] | None:
        """不是本人的会话返回 None，调用方回 404。

        q 非空时只返回命中的 user 消息（单次会话量小，顺序扫描即可，
        中文上 pg_trgm 本来就失效，见计划步骤 5.3）。
        """
        if not self.owns(user_id, session_id):
            return None
        sid = str(uuid.UUID(str(session_id)))
        sql = (
            "SELECT id, role, content, created_at, meta, turn_no FROM message "
            "WHERE session_id = %s"
        )
        args: list[Any] = [sid]
        if q.strip():
            sql += " AND role = 'user' AND content ILIKE %s"
            args.append(f"%{q.strip()}%")
        sql += " ORDER BY turn_no ASC, id ASC"
        with db.connect() as conn:
            rows = conn.execute(sql, tuple(args)).fetchall()
        messages = []
        for row in rows:
            item = {
                "id": row[0],
                "role": row[1],
                "content": row[2],
                "created_at": row[3],
                "turn_no": row[5],
            }
            meta = row[4] or {}
            if row[1] == "assistant" and isinstance(meta, dict):
                item.update(meta)
            messages.append(item)
        return messages

    def save_assistant_card(
        self,
        user_id: str,
        session_id: str,
        message_id: int,
        card: dict,
    ) -> None:
        """把补算出来的子图写回这条回答，下次打开不用再查图。"""
        if not self.owns(user_id, session_id):
            return
        with db.connect() as conn:
            conn.execute(
                """
                UPDATE message SET meta = %s
                WHERE id = %s AND session_id = %s AND role = 'assistant'
                """,
                (Jsonb(_json_card(card)), message_id, str(uuid.UUID(str(session_id)))),
            )

    def recent_questions(self, user_id: str, limit: int = RECENT_QUESTION_LIMIT) -> list[dict[str, Any]]:
        """最近的用户原话。点一下是再问一句，不是打开旧会话。"""
        with db.connect() as conn:
            rows = conn.execute(
                """
                SELECT m.content, m.created_at, s.id::text
                FROM message m
                JOIN session s ON s.id = m.session_id
                WHERE s.user_id = %s AND m.role = 'user'
                ORDER BY m.created_at DESC, m.id DESC
                LIMIT %s
                """,
                (user_id, limit),
            ).fetchall()
        return [
            {"question": row[0], "created_at": row[1], "session_id": row[2]}
            for row in rows
        ]

    def round_count(self, session_id: str) -> int:
        with db.connect() as conn:
            row = conn.execute(
                "SELECT COALESCE(MAX(turn_no), 0) FROM message WHERE session_id = %s",
                (session_id,),
            ).fetchone()
        return int(row[0]) if row else 0

    def compress_if_needed(self, session_id: str, summarize_fn=None) -> bool:
        """滚动摘要。触发只看轮数，写回必须**合并**旧摘要。

        两个容易做错的点：

        1. 只把增量交给模型再 `UPDATE summary=?` 覆盖 —— 第二次压缩会把
           上一次的摘要换掉，长会话里只剩最后一段增量。必须把 prev 一起给模型。
        2. 触发条件里塞 token 预算 —— 按 `cutoff = 轮数 - 保留数`，轮数 ≤ 保留数时
           没有可摘要的行，> 保留数时轮数条件已经触发，预算是个死条件。
           所以这里只按轮数触发（4.4 的答案维持"当前没有按阈值压缩"）。

        失败不推进水位线：库里的消息还在，下次重试即可，不阻断回答。
        """
        if not config.MEMORY_SUMMARY_ENABLED:
            return False
        if _compress_should_skip(session_id):
            return False
        retain = config.MEMORY_RETAIN_TURNS

        try:
            with db.connect() as conn:
                cur = conn.cursor()
                # NOWAIT：拿不到锁就跳过本轮，不为压缩让请求排队
                cur.execute(
                    "SELECT id FROM session WHERE id = %s FOR UPDATE NOWAIT", (session_id,)
                )
                if cur.fetchone() is None:
                    return False
                cur.execute(
                    "SELECT COALESCE(summary, ''), COALESCE(summary_until_msg_id, 0) "
                    "FROM session WHERE id = %s",
                    (session_id,),
                )
                prev_summary, waterline = cur.fetchone()
                cur.execute(
                    "SELECT COALESCE(MAX(turn_no), 0) FROM message WHERE session_id = %s",
                    (session_id,),
                )
                max_turn = int(cur.fetchone()[0])
                cutoff = max_turn - retain
                if cutoff <= 0:
                    return False
                cur.execute(
                    "SELECT role, content FROM message WHERE session_id = %s "
                    "  AND turn_no <= %s AND id > %s ORDER BY turn_no ASC, id ASC",
                    (session_id, cutoff, waterline),
                )
                rows = cur.fetchall()
                if not rows:
                    return False
                cur.execute(
                    "SELECT COALESCE(MAX(id), 0) FROM message "
                    "WHERE session_id = %s AND turn_no <= %s",
                    (session_id, cutoff),
                )
                new_waterline = int(cur.fetchone()[0])
        except Exception as exc:  # noqa: BLE001 —— 并发/读失败都不该影响回答
            log.warning("跳过本轮压缩：%s", exc)
            return False

        inc = "\n".join(f"{r[0]}: {r[1]}" for r in rows)
        fn = summarize_fn or _summarize
        try:
            merged = fn(str(prev_summary or ""), inc)
        except Exception as exc:  # noqa: BLE001
            log.warning("摘要失败，不推进水位线：%s", exc)
            _compress_failed(session_id)
            return False
        if not merged:
            return False

        # 写回时带上刚才读到的水位线。另一条压缩已经写过的话，这次放弃，
        # 避免用旧增量盖掉新摘要。锁不跨模型调用，所以不能靠行锁。
        with db.connect() as conn:
            cur = conn.execute(
                "UPDATE session SET summary = %s, summary_until_msg_id = %s, "
                "     summary_updated_at = now() "
                "WHERE id = %s AND COALESCE(summary_until_msg_id, 0) = %s",
                (merged, new_waterline, session_id, int(waterline)),
            )
            if cur.rowcount != 1:
                log.warning("摘要写回时水位线已变，放弃本次 session=%s", session_id)
                return False
        _compress_ok(session_id)
        return True

    def summarize_if_needed(self, session_id: str, summarize_fn=None) -> bool:
        """旧名字，留给可能的调用方。等价于 compress_if_needed。"""
        return self.compress_if_needed(session_id, summarize_fn)
