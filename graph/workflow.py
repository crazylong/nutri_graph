"""
graph/workflow.py —— LangGraph 工作流组装

流程：
  START → guard → normalize → link → route
    ├─ graph_query → route_after_graph
    │      ├─ assemble → generate → memory_write → END
    │      └─ vector_fallback → assemble → ...
    └─ vector_fallback → assemble → generate → memory_write → END

guard 命中红旗症状 → 直接 END（短路）
在线断言级接地还没做，所以不再挂一个空的 verify 节点。
"""
from __future__ import annotations

from langgraph.graph import StateGraph, END, START

from graph.state import RAGState
from graph.nodes import (
    guard_node, normalize_node, link_node, route_node,
    graph_query_node, vector_fallback_node, assemble_node,
    generate_node, memory_write_node, uncatalogued_node,
    route_after_link, route_after_graph,
)


def _route_after_guard(state: dict) -> str:
    # guard 若已产出 answer，说明命中红旗症状，直接结束
    if state.get("answer"):
        return "end"
    return "continue"


def build_workflow() -> StateGraph:
    wf = StateGraph(RAGState)

    wf.add_node("guard", guard_node)
    wf.add_node("normalize", normalize_node)
    wf.add_node("link", link_node)
    wf.add_node("route", route_node)
    wf.add_node("graph_query", graph_query_node)
    wf.add_node("vector_fallback", vector_fallback_node)
    wf.add_node("uncatalogued", uncatalogued_node)
    wf.add_node("assemble", assemble_node)
    wf.add_node("generate", generate_node)
    wf.add_node("memory_write", memory_write_node)

    wf.add_edge(START, "guard")
    wf.add_conditional_edges("guard", _route_after_guard, {
        "end": END,
        "continue": "normalize",
    })
    wf.add_edge("normalize", "link")
    wf.add_edge("link", "route")
    wf.add_conditional_edges("route", route_after_link, {
        "graph_query": "graph_query",
        "uncatalogued": "uncatalogued",
        "vector_fallback": "vector_fallback",
    })

    # 未收录：回答是确定性的，不经过生成，直接落记忆后结束
    wf.add_edge("uncatalogued", "memory_write")

    wf.add_conditional_edges("graph_query", route_after_graph, {
        "assemble": "assemble",
        "vector_fallback": "vector_fallback",
    })
    wf.add_edge("vector_fallback", "assemble")

    wf.add_edge("assemble", "generate")
    wf.add_edge("generate", "memory_write")
    wf.add_edge("memory_write", END)

    return wf


_graph = None


def get_agent():
    global _graph
    if _graph is None:
        _graph = build_workflow().compile()
    return _graph
