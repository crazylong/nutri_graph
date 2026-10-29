"""
memory/long_term.py —— 长期记忆（PostgreSQL + pgvector）

UserProfile 已接入问答：过敏和忌口会进 Cypher 过滤。
LongTermMemory（memory_fact 的向量召回）还没有调用方，问答链路不读不写它。
"""
from __future__ import annotations

from typing import Any

import psycopg

import config

import db


_ARRAY_FIELDS = ("allergies", "conditions", "avoid_list", "hobbies", "symptoms")
_SCALAR_FIELDS = ("diet_style", "age", "gender", "height_cm", "weight_kg")


def _empty_profile() -> dict[str, Any]:
    return {
        "allergies": [],
        "conditions": [],
        "diet_style": "",
        "avoid_list": [],
        "age": None,
        "gender": "",
        "height_cm": None,
        "weight_kg": None,
        "hobbies": [],
        "symptoms": [],
    }


def _clean_list(value: Any) -> list[str]:
    if not isinstance(value, list):
        return []
    out: list[str] = []
    for item in value:
        if isinstance(item, str) and item.strip() and item.strip() not in out:
            out.append(item.strip())
    return out


class UserProfile:
    """用户画像：过敏 / 慢病 / 忌口，加上人口学与症状。"""

    def __init__(self, dsn: str = None):
        # 同 ShortTermMemory：dsn 只留兼容，实际连的是 db.connect() 的池
        # （按 config.PG_DSN 建）。传别的库不会生效。
        self.dsn = dsn or config.PG_DSN
        if dsn and dsn != config.PG_DSN:
            log.warning(
                "UserProfile 收到与 config.PG_DSN 不同的 DSN，仍会连 config.PG_DSN"
            )

    def get(self, user_id: str) -> dict[str, Any]:
        with db.connect() as conn:
            cur = conn.cursor()
            cur.execute(
                "SELECT allergies, conditions, diet_style, avoid_list, "
                "age, gender, height_cm, weight_kg, hobbies, symptoms "
                "FROM user_profile WHERE user_id = %s",
                (user_id,),
            )
            row = cur.fetchone()
        if not row:
            return _empty_profile()
        return {
            "allergies": row[0] or [],
            "conditions": row[1] or [],
            "diet_style": row[2] or "",
            "avoid_list": row[3] or [],
            "age": row[4],
            "gender": row[5] or "",
            "height_cm": row[6],
            "weight_kg": row[7],
            "hobbies": row[8] or [],
            "symptoms": row[9] or [],
        }

    def update(self, user_id: str, **fields: Any) -> None:
        """本轮写入：标量非空才覆盖，数组只做并集，空数组不写。"""
        scalars: dict[str, Any] = {}
        arrays: dict[str, list[str]] = {}
        for key, value in fields.items():
            if key in _ARRAY_FIELDS:
                cleaned = _clean_list(value)
                if cleaned:
                    arrays[key] = cleaned
            elif key in _SCALAR_FIELDS and _scalar_present(value):
                scalars[key] = value
        if not scalars and not arrays:
            return
        # DO UPDATE SET 用 EXCLUDED. 引用新值，避免重复占位符：
        # 否则 INSERT 用 N 个、UPDATE 又用 N 个，占位符数 = 2N+1，参数只有 N+1。
        cols = [*scalars.keys(), *arrays.keys()]
        params: list[Any] = [user_id, *scalars.values(), *arrays.values()]
        assignments = [f"{col} = EXCLUDED.{col}" for col in scalars]
        assignments.extend(
            f"{col} = ARRAY(SELECT DISTINCT unnest("
            f"COALESCE(user_profile.{col}, '{{}}') || COALESCE(EXCLUDED.{col}, '{{}}')))"
            for col in arrays
        )
        placeholders = ", ".join(["%s"] * len(cols))
        sql = (
            f"INSERT INTO user_profile (user_id, {', '.join(cols)}) "
            f"VALUES (%s, {placeholders}) "
            f"ON CONFLICT (user_id) DO UPDATE SET {', '.join(assignments)}, updated_at = now()"
        )
        with db.connect() as conn:
            conn.execute(sql, params)


def _scalar_present(value: Any) -> bool:
    if value is None or isinstance(value, bool):
        return False
    if isinstance(value, str):
        return bool(value.strip())
    if isinstance(value, (int, float)) and value == 0:
        return False
    return True


class LongTermMemory:
    """memory_fact 语义召回。问答链路未使用。"""

    def __init__(self, dsn: str = None, embedding_fn=None):
        self.dsn = dsn or config.PG_DSN
        self.embedding_fn = embedding_fn

    def add_fact(self, user_id: str, content: str, fact_type: str = "", confidence: float = 0.5) -> None:
        emb = None
        if self.embedding_fn:
            emb = self.embedding_fn(content)
        with db.connect() as conn:
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
        with db.connect() as conn:
            cur = conn.cursor()
            cur.execute(
                "SELECT content FROM memory_fact WHERE user_id = %s "
                "ORDER BY embedding <=> %s LIMIT %s",
                (user_id, str(qvec), top_k),
            )
            rows = cur.fetchall()
        return [r[0] for r in rows]
