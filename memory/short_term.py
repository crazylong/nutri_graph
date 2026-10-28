"""
memory/short_term.py —— 短期记忆（PostgreSQL）

短期 = 最近 N 轮 message + session 滚动摘要。
"""
from __future__ import annotations

import json
import uuid
from typing import Any

import psycopg
from psycopg.types.json import Jsonb

import config

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


class ShortTermMemory:
    def __init__(self, dsn: str = None):
        self.dsn = dsn or config.PG_DSN

    def _conn(self):
        return psycopg.connect(self.dsn)

    def create_session(self, user_id: str) -> str:
        """总是新开一会话。不复用 ended_at 为空的旧会话。"""
        sid = str(uuid.uuid4())
        with self._conn() as conn:
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
        with self._conn() as conn:
            row = conn.execute(
                "SELECT 1 FROM session WHERE id = %s AND user_id = %s",
                (sid, user_id),
            ).fetchone()
        return row is not None

    def load_context(self, session_id: str, n: int = None) -> str:
        """加载最近 N 轮消息作为上下文。"""
        n = n or config.SHORT_TERM_WINDOW
        with self._conn() as conn:
            cur = conn.cursor()
            cur.execute(
                "SELECT role, content FROM message WHERE session_id = %s "
                "ORDER BY id DESC LIMIT %s",
                (session_id, n),
            )
            rows = cur.fetchall()[::-1]
        return "\n".join(f"{r[0]}: {r[1]}" for r in rows)

    def save_round(
        self,
        session_id: str,
        question: str,
        answer: str,
        card: dict | None = None,
    ) -> None:
        title = " ".join((question or "").split())[:SESSION_TITLE_CHARS]
        with self._conn() as conn:
            conn.execute(
                "INSERT INTO message (session_id, role, content) VALUES (%s, 'user', %s)",
                (session_id, question),
            )
            conn.execute(
                "INSERT INTO message (session_id, role, content, meta) VALUES (%s, 'assistant', %s, %s)",
                (session_id, answer, Jsonb(_json_card(card))),
            )
            # 标题只用首条用户问题。之后的轮次不再改。
            if title:
                conn.execute(
                    "UPDATE session SET title = %s "
                    "WHERE id = %s AND COALESCE(title, '') = ''",
                    (title, session_id),
                )

    def list_sessions(self, user_id: str, limit: int = SESSION_LIST_LIMIT) -> list[dict[str, Any]]:
        """按最后一条消息时间倒序。没有消息时用 started_at。"""
        with self._conn() as conn:
            rows = conn.execute(
                """
                SELECT s.id::text,
                       COALESCE(s.title, ''),
                       s.started_at,
                       COALESCE(MAX(m.created_at), s.started_at) AS last_at,
                       COUNT(m.id)
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
            }
            for row in rows
        ]

    def list_messages(self, user_id: str, session_id: str) -> list[dict[str, Any]] | None:
        """不是本人的会话返回 None，调用方回 404。"""
        if not self.owns(user_id, session_id):
            return None
        with self._conn() as conn:
            rows = conn.execute(
                """
                SELECT id, role, content, created_at, meta
                FROM message
                WHERE session_id = %s
                ORDER BY created_at ASC, id ASC
                """,
                (str(uuid.UUID(str(session_id))),),
            ).fetchall()
        messages = []
        for row in rows:
            item = {
                "id": row[0],
                "role": row[1],
                "content": row[2],
                "created_at": row[3],
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
        with self._conn() as conn:
            conn.execute(
                """
                UPDATE message SET meta = %s
                WHERE id = %s AND session_id = %s AND role = 'assistant'
                """,
                (Jsonb(_json_card(card)), message_id, str(uuid.UUID(str(session_id)))),
            )

    def recent_questions(self, user_id: str, limit: int = RECENT_QUESTION_LIMIT) -> list[dict[str, Any]]:
        """最近的用户原话。点一下是再问一句，不是打开旧会话。"""
        with self._conn() as conn:
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

    def summarize_if_needed(self, session_id: str, summarize_fn) -> None:
        """超长时滚动摘要（summarize_fn(history) -> summary）。"""
        with self._conn() as conn:
            cur = conn.cursor()
            cur.execute("SELECT count(*) FROM message WHERE session_id = %s", (session_id,))
            n = cur.fetchone()[0]
        if n >= config.SESSION_SUMMARY_THRESHOLD:
            history = self.load_context(session_id, config.SESSION_SUMMARY_THRESHOLD)
            summary = summarize_fn(history) if summarize_fn else history[:200]
            with self._conn() as conn:
                conn.execute(
                    "UPDATE session SET summary = %s WHERE id = %s", (summary, session_id)
                )
