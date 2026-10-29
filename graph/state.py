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
    # 当轮网页参考。必须声明，否则 LangGraph 会把这个键丢掉，
    # /chat 和历史卡片都看不到来源。
    _web_answer: dict[str, Any]

    # 意图与检索
    _intents: list[str]
    _graph_results: list[dict[str, Any]]
    _vector_results: list[dict[str, Any]]
    _graph_hit: bool
    _degraded: bool                        # 是否降级到纯向量

    # 记忆
    _memory_context: str
    _user_profile: dict[str, Any]
    # 命中敏感词：memory_write 见到它就只打码用户侧 + 会话标题，并跳过画像抽取。
    # ⚠️ 必须在这里声明：RAGState 是 TypedDict，没声明的键会被 LangGraph 丢掉，
    #    节点里能读到、下一个节点读不到（这就是打码不生效的原因）。
    _redact: bool
    # political：trace 的 input 和 output 都换成占位。
    # guard：红旗路径跳过了 sensitive_guard，只换 input，就医回答留在 output。
    # 同样必须声明，否则最终状态里没有 kind，缺省会把就医回答也盖成占位。
    _redacted_kind: str

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
