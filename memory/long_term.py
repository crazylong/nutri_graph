"""
memory/long_term.py —— 长期记忆（PostgreSQL + pgvector）

长期 = 用户画像（硬约束，进 Cypher 过滤）+ memory_fact 语义召回。
"""
from __future__ import annotations

from typing import Any

import psycopg

import config


class UserProfile:
    """用户画像：过敏 / 慢病 / 饮食偏好 / 忌口。硬约束。"""

    def __init__(self, dsn: str = None):
        self.dsn = dsn or config.PG_DSN

    def get(self, user_id: str) -> dict[str, Any]:
        with psycopg.connect(self.dsn) as conn:
            cur = conn.cursor()
            cur.execute(
                "SELECT allergies, conditions, diet_style, avoid_list FROM user_profile "
                "WHERE user_id = %s",
                (user_id,),
            )
            row = cur.fetchone()
        if not row:
            return {"allergies": [], "conditions": [], "diet_style": "", "avoid_list": []}
        return {
            "allergies": row[0] or [],
            "conditions": row[1] or [],
            "diet_style": row[2] or "",
            "avoid_list": row[3] or [],
        }

    def update(self, user_id: str, **fields: Any) -> None:
        allowed = {"allergies", "conditions", "diet_style", "avoid_list"}
        sets = {k: v for k, v in fields.items() if k in allowed}
        if not sets:
            return
        # DO UPDATE SET 用 EXCLUDED. 引用新值，避免重复占位符：
        # 否则 INSERT 用 N 个、UPDATE 又用 N 个，占位符数 = 2N+1，参数只有 N+1，
        # 报「7 placeholders but 4 parameters」。
        cols = ", ".join(sets)
        updates = ", ".join(f"{k} = EXCLUDED.{k}" for k in sets)
        vals = list(sets.values())
        with psycopg.connect(self.dsn) as conn:
            conn.execute(
                f"INSERT INTO user_profile (user_id, {cols}) VALUES (%s, "
                + ", ".join(["%s"] * len(sets))
                + f") ON CONFLICT (user_id) DO UPDATE SET {updates}, updated_at = now()",
                (user_id, *vals),
            )


class LongTermMemory:
    """长期事实语义召回（pgvector）。"""

    def __init__(self, dsn: str = None, embedding_fn=None):
        self.dsn = dsn or config.PG_DSN
        self.embedding_fn = embedding_fn

    def add_fact(self, user_id: str, content: str, fact_type: str = "", confidence: float = 0.5) -> None:
        emb = None
        if self.embedding_fn:
            emb = self.embedding_fn(content)
        with psycopg.connect(self.dsn) as conn:
            conn.execute(
                "INSERT INTO memory_fact (user_id, fact_type, content, embedding, confidence) "
                "VALUES (%s, %s, %s, %s, %s)",
                (user_id, fact_type, content, emb, confidence),
            )

    def search(self, user_id: str, query: str, top_k: int = None) -> list[str]:
        if not self.embedding_fn:
            return []
        top_k = top_k or config.MEMORY_FACT_TOP_K
        qvec = self.embedding_fn(query)
        with psycopg.connect(self.dsn) as conn:
            cur = conn.cursor()
            cur.execute(
                "SELECT content FROM memory_fact WHERE user_id = %s "
                "ORDER BY embedding <=> %s LIMIT %s",
                (user_id, str(qvec), top_k),
            )
            rows = cur.fetchall()
        return [r[0] for r in rows]
