"""
memory/short_term.py —— 短期记忆（PostgreSQL）

短期 = 最近 N 轮 message + session 滚动摘要。
"""
from __future__ import annotations

import uuid
from typing import Any

import psycopg

import config


class ShortTermMemory:
    def __init__(self, dsn: str = None):
        self.dsn = dsn or config.PG_DSN

    def _conn(self):
        return psycopg.connect(self.dsn)

    def get_session(self, user_id: str) -> str:
        """获取或创建会话 id。"""
        with self._conn() as conn:
            cur = conn.cursor()
            cur.execute(
                "SELECT id FROM session WHERE user_id = %s AND ended_at IS NULL "
                "ORDER BY started_at DESC LIMIT 1",
                (user_id,),
            )
            row = cur.fetchone()
            if row:
                return str(row[0])
            sid = str(uuid.uuid4())
            conn.execute("INSERT INTO session (id, user_id) VALUES (%s, %s)", (sid, user_id))
            return sid

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

    def save_round(self, session_id: str, question: str, answer: str) -> None:
        with self._conn() as conn:
            conn.execute(
                "INSERT INTO message (session_id, role, content) VALUES (%s, 'user', %s)",
                (session_id, question),
            )
            conn.execute(
                "INSERT INTO message (session_id, role, content) VALUES (%s, 'assistant', %s)",
                (session_id, answer),
            )

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
