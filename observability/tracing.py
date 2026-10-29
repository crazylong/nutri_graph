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
    """为一次问答创建 trace 上下文。

    两处和敏感词有关，都别改回去：

    1. `input` 在进图**之前**就写了原句。图内打码发生在之后，
       所以命中时调用方要显式调 `redact_trace()` 把 input/output 一起改成占位
       —— 只改 message 表等于"库里没有、日志里有"。
    2. 原来 `finally` 里还用原句写了一次 `output`，会**盖掉** `record_result`
       刚写上去的回答。那是既有问题，顺手去掉了。
    """
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
        pass


def redact_trace(trace, placeholder: str, keep_output: bool = False) -> None:
    """命中后把这条 trace 的 input 换成占位。

    keep_output 只给红旗就医回答：output 要留下就医原文。
    kind 缺省时 input 和 output 都写占位。
    """
    if trace is None:
        return
    try:
        payload: dict[str, Any] = {"input": {"question": placeholder}}
        if not keep_output:
            payload["output"] = {"answer": placeholder}
        trace.update(**payload)
    except Exception:  # noqa: BLE001 —— 埋点失败不能影响回答
        pass


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
