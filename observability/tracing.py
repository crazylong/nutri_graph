"""
observability/tracing.py —— Langfuse 追踪封装

设计要点（本项目的差异化用法）：
  - 把 evidence_counts / graph_hit / degraded 写进 trace metadata，
    便于在 Langfuse 里直接筛「A 级证据为 0」和「走了降级向量」的 query
  - Ragas 分数回写为 trace score
"""
from __future__ import annotations

import logging
from contextlib import contextmanager
from typing import Any

import config

log = logging.getLogger("observability.tracing")

_client = None


def get_client():
    global _client
    if not config.LANGFUSE_ENABLED:
        return None
    if _client is None and config.LANGFUSE_PUBLIC_KEY and config.LANGFUSE_SECRET_KEY:
        from langfuse import Langfuse

        _client = Langfuse(
            public_key=config.LANGFUSE_PUBLIC_KEY,
            secret_key=config.LANGFUSE_SECRET_KEY,
            host=config.LANGFUSE_HOST,
        )
    return _client


@contextmanager
def trace(question: str, user_id: str = "anonymous"):
    """为一次问答创建 trace 上下文。"""
    client = get_client()
    if client is None:
        yield None
        return
    trace = client.trace(
        name="nutri_graph.chat",
        input={"question": question},
        user_id=user_id,
    )
    try:
        yield trace
    finally:
        trace.update(output={"question": question})


def record_span(
    trace,
    name: str,
    metadata: dict[str, Any] | None = None,
    input: dict[str, Any] | None = None,
    output: dict[str, Any] | None = None,
):
    """在 trace 下记录一个 span（节点级）。"""
    if trace is None:
        return None
    span = trace.span(name=name, input=input, output=output, metadata=metadata)
    return span


def record_result(
    trace,
    answer: str,
    evidence_counts: dict[str, int],
    graph_hit: bool,
    degraded: bool,
    elapsed_ms: float,
):
    """把关键结果写进 trace（含证据分布 metadata）。"""
    if trace is None:
        return
    trace.update(
        output={"answer": answer[:500]},
        metadata={
            "evidence_counts": evidence_counts,
            "graph_hit": graph_hit,
            "degraded": degraded,
            "elapsed_ms": elapsed_ms,
        },
    )


def record_score(trace_id: str, name: str, value: float):
    """回写单个评估分数为 trace score。"""
    client = get_client()
    if client is None or trace_id is None:
        return
    client.score(trace_id=trace_id, name=name, value=value)
