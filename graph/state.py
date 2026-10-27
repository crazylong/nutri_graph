"""
graph/state.py —— LangGraph State 定义
"""
from __future__ import annotations

from typing import Annotated, Any, Optional

from typing_extensions import TypedDict
import operator


class RAGState(TypedDict, total=False):
    # 用户输入
    question: str
    user_id: str
    session_id: str

    # 归一与链接
    _normalized: str
    _entities: list[dict[str, Any]]       # 链接到的实体 [{canonical_id, label, name, score, method}]
    _unresolved: list[str]
    _near_misses: list[dict[str, Any]]    # 未收录词的近似候选（「你是不是想问黑豆？」）
    _link_residual: str                   # 没被任何别名占用的原文片段
    _food_requests: list[dict[str, Any]]  # 未收录食材的登记结果

    # 意图与检索
    _intents: list[str]
    _graph_results: list[dict[str, Any]]
    _vector_results: list[dict[str, Any]]
    _graph_hit: bool
    _degraded: bool                        # 是否降级到纯向量

    # 记忆
    _memory_context: str
    _user_profile: dict[str, Any]

    # 生成
    answer: str
    basis: list[dict[str, Any]]
    gaps: list[str]
    confidence: float

    # 追踪
    _evidence_counts: dict[str, int]
    _elapsed_ms: float


# 供 operator.add 累加用的字段（如需多节点追加历史）
History = Annotated[list[str], operator.add]
