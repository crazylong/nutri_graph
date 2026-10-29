"""
graph/nodes.py —— LangGraph 节点实现

节点链：
  guard → normalize → link → route → graph_query / vector_fallback → assemble → generate → memory_write
"""
from __future__ import annotations

import json
import logging
import threading
import time
from typing import Any

import config
import db
from kg.query import GraphQuery, detect_intent, plan_queries
from kg.normalize import EntityLinker, get_alias_store, normalize_text
from graph.boundary import mentions_health_constraint, parse_profile_from_answer

log = logging.getLogger("graph.nodes")

_query = GraphQuery()


# ============================================================
# guard —— 安全前置（红旗症状拦截）
# ============================================================
def guard_node(state: dict) -> dict:
    q = state["question"]
    for sym in config.RED_FLAG_SYMPTOMS:
        if sym in q:
            return {
                "answer": (
                    f"你提到的「{sym}」属于需要及时就医的情况，食疗无法处理。"
                    "请尽快前往医院就诊，不要依赖食疗或自行用药。"
                ),
                "_graph_hit": False,
                "_degraded": False,
                "confidence": 1.0,
            }
    return {}


# ============================================================
# normalize —— 归一 + 指代消解（简化版：仅归一，多轮指代由 memory 补全）
# ============================================================
def normalize_node(state: dict) -> dict:
    q = state["question"]
    return {"_normalized": normalize_text(q)}


# ============================================================
# link —— 实体链接（字典最长匹配 + 模糊 + 未收录候选）
# ============================================================
def link_node(state: dict) -> dict:
    from kg.request import extract_candidates

    alias_store = get_alias_store()
    linker = EntityLinker(alias_store, vector_search=_entity_vector_search)

    # 整句走字典最长匹配。没被别名盖住的片段再逐个走模糊和向量，
    # 这两级在 EntityLinker.link 里，阈值读 ENTITY_LINK_THRESHOLD。
    norm = state.get("_normalized") or state.get("question", "")
    result = linker.link_question(norm)

    candidates = extract_candidates(result.residual)
    entities = list(result.entities)
    unresolved: list[str] = []
    for term in candidates:
        extra = linker.link(term)
        if extra.entities:
            entities.extend(extra.entities)
        else:
            unresolved.append(term)

    seen: set[tuple[str, str]] = set()
    deduped = []
    for entity in entities:
        key = (entity.label, entity.canonical_id)
        if key in seen:
            continue
        seen.add(key)
        deduped.append(entity)

    near = linker.near_misses(unresolved) if unresolved else []
    return {
        "_entities": [e.__dict__ for e in deduped],
        "_unresolved": unresolved,
        "_near_misses": near,
        "_link_residual": result.residual,
    }


def _entity_vector_search(query: str, top_k: int = 3) -> list[dict]:
    """实体链接的向量兜底：把查询文本转 embedding 后走 Milvus。

    EntityLinker 只在字典精确/模糊都未命中时才调它，所以这里的失败
    （Milvus 没起 / embedding 失败）不该让整个链路崩 —— 返回空即可。
    """
    try:
        from rag.client import search_entity
        from rag.embed import get_embeddings

        vec = get_embeddings().embed_query(query)
        return search_entity(vec, top_k=top_k)
    except Exception as exc:  # noqa: BLE001
        log.warning("实体向量兜底不可用，跳过：%s: %s", type(exc).__name__, exc)
        return []


# ============================================================
# uncatalogued —— 未收录食材：登记 + 明确回话
# ============================================================
def uncatalogued_node(state: dict) -> dict:
    """图谱里没有这个食材时的出口。

    为什么不走 generate：这里没有图事实可依据，让模型自由发挥正是要避免的事。
    回答是确定性的，而且必须同时做到三件事 ——
      ① 明说暂未收录（不能装懂）
      ② 不给营养数据 / 搭配建议（不能编造）
      ③ 给出可执行的下一步（已登记、编号、大约多久）
    """
    from kg.request import register

    who = state.get("user_id", "anonymous")
    terms = (state.get("_unresolved") or [])[:2]   # 一句话里最多登记两个，防刷
    near = state.get("_near_misses") or []

    results = []
    for term in terms:
        term_near = [n for n in near if n.get("mention") == term]
        results.append(register(term, who=who, source="chat", near_misses=term_near))

    if not results:
        answer = "这个词我暂时没能对应到具体的食材。能说得再具体一点吗，比如「黑豆」？"
    else:
        answer = "\n".join(r.message for r in results)

    return {
        "answer": answer,
        "basis": [],
        "gaps": [f"图谱未收录：{r.food_key or r.term}" for r in results] or ["未能识别食材"],
        "confidence": 0.0,
        "_graph_hit": False,
        "_degraded": False,
        "_food_requests": [
            {"term": r.term, "food_key": r.food_key, "action": r.action,
             "request_id": r.request_id, "status": r.status, "hit_count": r.hit_count}
            for r in results
        ],
    }


# ============================================================
# route —— 意图检测 + 跳数选择
# ============================================================
def route_node(state: dict) -> dict:
    intents = detect_intent(state["question"])
    # 画像在分叉之前加载。图查询要拿它做过敏过滤，
    # 降级和未收录路径也要在生成时用到年龄和症状。
    # 用户拒绝收集健康信息时不读画像，回答保持通用。
    profile = {} if state.get("consent_profile") is False else _load_profile(state)
    return {"_intents": intents, "_user_profile": profile}


def _load_profile(state: dict) -> dict:
    existing = state.get("_user_profile")
    if existing:
        return existing
    if not config.MEMORY_ENABLED:
        return {}
    try:
        from memory.long_term import UserProfile

        return UserProfile().get(state.get("user_id") or "anonymous")
    except db.PoolBusy:
        # 池打满不能当成"这个人没有画像"。
        # 画像空着继续走下去，过敏和忌口的排除名单就是空的 ——
        # 用户拿到的是一条没排除过敏原的食疗建议，而且返回 200。
        # 这里必须让它冒出去，由 app.py 的处理器变成 503。
        raise
    except Exception as exc:  # noqa: BLE001
        # 库真的不可用（连不上、表没建）是另一类故障：按"没有画像"继续回答，
        # 降级的是个性化程度，不是安全性判断的那条链。
        log.warning("加载画像失败：%s", exc)
        return {}


def route_after_link(state: dict) -> str:
    """有能落地的 (意图, 节点) 才走图。

    食材问法要有 Ingredient，症状问法要有 Symptom。
    对得上 → graph_query
    对不上、但有未收录词 → uncatalogued
    其余 → vector_fallback
    """
    entities = state.get("_entities") or []
    intents = state.get("_intents") or ["pair"]
    if plan_queries(entities, intents):
        return "graph_query"
    if state.get("_unresolved"):
        return "uncatalogued"
    return "vector_fallback"


# ============================================================
# graph_query —— 按意图跑 Cypher（含用户画像硬过滤）
# ============================================================
def graph_query_node(state: dict) -> dict:
    results = []
    evidence_counts: dict[str, int] = {}
    profile = state.get("_user_profile") or {}
    allergens = list(profile.get("allergies", [])) + list(profile.get("avoid_list", []))

    plans = plan_queries(state.get("_entities") or [], state.get("_intents") or [])
    for plan in plans:
        intent = plan["intent"]
        allowed = [
            x.strip()
            for x in config.EVIDENCE_POLICY_MAP.get(intent, "").split(",")
            if x.strip()
        ]
        rows = _query.run(
            intent,
            plan["canonical_id"],
            allowed,
            allergens,
            include_provisional=config.INCLUDE_PROVISIONAL,
        )
        for r in rows:
            # 打上来源锚点：一句话里可能有多个食材（「西红柿和牛肉能一起吃吗」），
            # 前端画子图时要知道这条边挂在哪个食材上。
            r["intent"] = intent
            r["_src_cid"] = plan["canonical_id"]
            r["_src_label"] = plan["label"]
            r["_src_name"] = next(
                (
                    (e.get("matched_alias") or e.get("name"))
                    for e in (state.get("_entities") or [])
                    if e.get("canonical_id") == plan["canonical_id"]
                ),
                plan["canonical_id"],
            )
            results.append(r)
            lv = r.get("elevel", "")
            evidence_counts[lv] = evidence_counts.get(lv, 0) + 1

    graph_hit = bool(results)
    provisional_count = sum(1 for r in results if r.get("provisional"))
    return {
        "_graph_results": results,
        "_graph_hit": graph_hit,
        "_degraded": False,
        "_evidence_counts": evidence_counts,
        "_provisional_count": provisional_count,
        "_user_profile": profile,
    }


def route_after_graph(state: dict) -> str:
    if state.get("_graph_hit"):
        return "assemble"
    return "vector_fallback"


# ============================================================
# vector_fallback —— 降级纯向量检索
# ============================================================
def vector_fallback_node(state: dict) -> dict:
    """词图没结果时的向量降级。

    这里**必须容错**：向量检索依赖 Milvus + 外部 embedding 服务，
    任何一个不可用都不该让整个 /chat 变成 500。
    降级也失败的正确语义是「没有确切依据」（弃答），不是「服务器错误」——
    用户看到的应该是「我没找到依据」，而不是一个空白页。
    实测踩过：Milvus 没起 + langchain-openai 的 token 化请求被 DashScope 拒绝，
    一个「黑豆和什么一起吃好」直接 500。
    """
    try:
        from rag.client import search_chunks
        from rag.embed import get_embeddings

        emb = get_embeddings()
        vec = emb.embed_query(state["question"])
        chunks = search_chunks(vec, top_k=config.CHUNK_TOP_K)
        chunks = [c for c in chunks if c["score"] >= config.CHUNK_THRESHOLD]
        return {"_vector_results": chunks, "_degraded": True}
    except Exception as exc:  # noqa: BLE001
        log.warning("向量降级不可用，按「无依据」处理：%s: %s", type(exc).__name__, exc)
        return {"_vector_results": [], "_degraded": True, "_vector_error": str(exc)[:200]}


# ============================================================
# assemble —— 上下文组装（结构化事实 + 证据标签）
# ============================================================
def assemble_node(state: dict) -> dict:
    # 组装成一个结构化上下文字符串，供 generate 使用
    facts = state.get("_graph_results", [])
    if not facts and state.get("_vector_results"):
        # 降级路径：向量结果直接作为上下文
        ctx = "\n".join(f"[文档] {c['text']}" for c in state["_vector_results"])
        return {"_memory_context": ctx}  # 复用字段暂存上下文

    lines = []
    for f in facts:
        name = f.get("name") or f.get("cid")
        etype = f.get("etype", "")
        elevel = f.get("elevel", "")
        claim = f.get("claim", "")
        # 单源补录的数据必须在这里显式标出来。上下文里混进硬证据，
        # 模型就会当成 A/B 级事实复述，前面做的证据分层全白费。
        tag = f"[{etype}/{elevel}{'·单源' if f.get('provisional') else ''}]"
        lines.append(f"{tag} {name}：{claim}".strip("："))
    ctx = "\n".join(lines)
    return {"_memory_context": ctx}


# ============================================================
# generate —— 严格接地生成（三档回答）
# ============================================================
def generate_node(state: dict) -> dict:
    from graph.boundary import (
        classify_tier,
        build_document_prompt,
        build_grounded_prompt,
        NONE_ANSWER,
        WEAK_DISCLAIMER,
        PROVISIONAL_DISCLAIMER,
    )

    facts = state.get("_graph_results", [])
    vector_results = state.get("_vector_results", [])

    # 图无命中且向量也无 → 弃答
    if not facts and not vector_results:
        # 但要把「食材不认识」和「食材认识、只是没有这方面的证据」分开说。
        # 都回一句「没有找到确切依据」的话，用户会以为食材没收录，转头又登记一次。
        known = [
            e.get("matched_alias") or e.get("name")
            for e in (state.get("_entities") or [])
            if e.get("label") == "Ingredient"
        ]
        if known:
            answer = (
                f"「{'、'.join(dict.fromkeys(known))}」已经收录，"
                "但我暂时没有能回答这个问题的确切依据 —— "
                "图谱里有它的基础信息，还缺这方面的证据。"
                "我不想凭常识推测，所以这里不给建议。"
            )
            gaps = [f"缺少「{'、'.join(dict.fromkeys(known))}」在该问题上的证据"]
        else:
            answer = NONE_ANSWER
            gaps = ["无检索证据"]
        return {
            "answer": answer,
            "basis": [],
            "gaps": gaps,
            "confidence": 0.0,
        }

    # 有图事实：按证据等级分档
    evidence_counts = state.get("_evidence_counts", {})
    tier = classify_tier(evidence_counts) if facts else "none"

    # 全部事实都来自单源补录 → 无论等级怎么标，都只能算「弱证据」。
    # 这不是多加一层惩罚，而是承认「一个人测出来的数」跟「两份资料互证」不是一回事。
    all_provisional = bool(facts) and all(f.get("provisional") for f in facts)
    if all_provisional and tier == "strong":
        tier = "weak"

    note = ""
    if tier == "weak":
        note = PROVISIONAL_DISCLAIMER if all_provisional else WEAK_DISCLAIMER

    profile = state.get("_user_profile") or {}
    history = _recent_dialogue(state)

    # 图 0 命中、向量已高于阈值：按文档转述，并标明不是图谱结论。
    # 低于阈值的块在 vector_fallback 里已经丢掉，到不了这里。
    if not facts and vector_results:
        if not config.DASHSCOPE_API_KEY:
            first = vector_results[0].get("text", "")
            return {
                "answer": f"（占位回答）检索到文档，不是图谱结论：{first}",
                "basis": [],
                "gaps": [],
                "confidence": 0.4,
                "_degraded": True,
            }
        prompt = build_document_prompt(state["question"], vector_results, profile, history)
        return _complete_from_prompt(prompt, degraded=True)

    prompt = build_grounded_prompt(
        state["question"], facts, tier, profile,
        provisional=all_provisional, history=history,
    )

    if not config.DASHSCOPE_API_KEY:
        answer = _dummy_answer(state.get("_memory_context", ""))
        if note:
            answer = note + "\n" + answer
        return {
            "answer": answer,
            "basis": [],
            "gaps": [],
            "confidence": 0.5,
        }
    return _complete_from_prompt(prompt, note=note)


def _recent_dialogue(state: dict) -> str:
    """当前会话最近几轮。没有 session_id 时不读，避免把别人的会话拉进来。"""
    if not config.MEMORY_ENABLED:
        return ""
    session_id = state.get("session_id")
    if not session_id:
        return ""
    try:
        from memory.short_term import ShortTermMemory

        return ShortTermMemory().load_context(session_id)
    except Exception as exc:  # noqa: BLE001
        log.warning("读取短期记忆失败：%s", exc)
        return ""


def _complete_from_prompt(prompt: str, note: str = "", degraded: bool = False) -> dict:
    """调模型池，把 JSON 回答拆开。池全不可用时明确说答不了。"""
    from llm import LLMUnavailableError, invoke_model

    try:
        resp = invoke_model(prompt, enable_thinking=False)
    except LLMUnavailableError as exc:
        log.warning("模型池不可用，降级作答：%s", exc)
        return {
            "answer": (
                "当前模型暂时都不可用（额度用尽或全部冷却中），无法生成回答。"
                "检索结果已就绪，稍后重试即可；如情况紧急请直接就医。"
            ),
            "basis": [],
            "gaps": ["模型暂时全部不可用"],
            "confidence": 0.0,
            "_degraded": True,
        }
    content = getattr(resp, "content", resp)
    try:
        obj = json.loads(content)
    except (json.JSONDecodeError, ValueError):
        obj = {"answer": content}
    answer = obj.get("answer", "")
    if note and note not in answer:
        answer = note + "\n" + answer
    out = {
        "answer": answer,
        "basis": obj.get("basis", []),
        "gaps": obj.get("gaps", []),
        "confidence": float(obj.get("confidence", 0.5)),
    }
    if degraded:
        out["_degraded"] = True
    return out


def _dummy_answer(ctx: str) -> str:
    """LLM 未接线时的占位回答（用于骨架验收）。"""
    first = ctx.split("\n")[0] if ctx else ""
    return f"（占位回答）基于图谱事实：{first}。请配置 DASHSCOPE_API_KEY 后获得完整回答。"


def _profile_llm():
    """画像抽取用的回退链。未配置 key 时返回 None，调用方跳过。"""
    if not config.DASHSCOPE_API_KEY:
        return None
    from llm import pool_chain

    return pool_chain(enable_thinking=False)


def _write_profile(user_id: str, question: str, answer: str) -> None:
    """后台抽过敏 / 慢病 / 忌口。失败只记日志，不影响已经返回的回答。"""
    try:
        from memory.long_term import UserProfile

        dialogue = f"用户：{question}\n助手：{answer}"
        updates = parse_profile_from_answer(dialogue, _profile_llm())
        if updates:
            UserProfile().update(user_id, **updates)
    except Exception as exc:  # noqa: BLE001 —— 画像是附加写入，不能回头影响问答
        log.warning("画像抽取失败：%s", exc)


def _defer_profile(user_id: str, question: str, answer: str) -> None:
    threading.Thread(
        target=_write_profile,
        args=(user_id, question, answer),
        daemon=True,
        name="profile-extract",
    ).start()


# ============================================================
# memory_write —— 落库（短期消息 + 画像增量抽取）
# ============================================================
def answer_card(state: dict) -> dict:
    """回答卡片上的结构化字段。和 /chat 用同一套翻译，历史回放才能把图画回来。"""
    from kg.subgraph import build_facts, build_subgraph

    results = state.get("_graph_results") or []
    return {
        "evidence_counts": state.get("_evidence_counts") or {},
        "graph_hit": bool(state.get("_graph_hit")),
        "facts": build_facts(results),
        "subgraph": build_subgraph(state.get("_entities") or [], results),
        "basis": state.get("basis") or [],
        "food_requests": state.get("_food_requests") or [],
        "disclaimer": config.DISCLAIMER,
    }


def retrieve_card(question: str, user_id: str) -> dict:
    """只重跑检索，不调生成模型。给还没存过子图的旧回答补一张图。"""
    state: dict = {"question": question, "user_id": user_id}
    for step in (normalize_node, link_node, route_node):
        state.update(step(state) or {})
    if route_after_link(state) == "graph_query":
        state.update(graph_query_node(state) or {})
    else:
        state["_graph_hit"] = False
        state["_graph_results"] = []
    return answer_card(state)


def memory_write_node(state: dict) -> dict:
    if not config.MEMORY_ENABLED:
        return {}
    try:
        from memory.short_term import ShortTermMemory

        user_id = state.get("user_id", "anonymous")
        stm = ShortTermMemory()
        # 不带 session_id 就新开一会话。不要复用「还没写 ended_at」的旧会话。
        session_id = state.get("session_id") or stm.create_session(user_id)
        try:
            card = answer_card(state)
        except Exception as exc:  # noqa: BLE001 —— 卡片翻译失败仍要留下文字
            log.warning("回答卡片翻译失败：%s", exc)
            card = {}
        stm.save_round(session_id, state["question"], state.get("answer", ""), card)

        # 画像要再打一次模型，而且不进当前这轮回答。放在请求里等，
        # 有图事实时用户会把这次耗时和生成叠在一起。没提到约束就直接跳过。
        question = state.get("question") or ""
        if (
            state.get("consent_profile", True)
            and config.DASHSCOPE_API_KEY
            and mentions_health_constraint(question)
        ):
            _defer_profile(user_id, question, state.get("answer", ""))
        return {"session_id": session_id}
    except Exception as e:  # noqa: BLE001 —— 记忆失败不阻断主流程
        log.warning("memory_write 失败：%s", e)
        return {}
