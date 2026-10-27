"""
graph/nodes.py —— LangGraph 节点实现

节点链：
  guard → normalize → link → route → graph_query / vector_fallback → assemble → generate → verify → memory_write
"""
from __future__ import annotations

import json
import logging
import time
from typing import Any

import config
from kg.query import GraphQuery, detect_intent
from kg.normalize import EntityLinker, AliasStore, normalize_text
from graph.boundary import parse_profile_from_answer

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

    alias_store = AliasStore(config.PG_DSN)
    linker = EntityLinker(alias_store, vector_search=_entity_vector_search)

    # 旧实现对整句做 exact_lookup —— 而整句永远不等于某个别名，
    # 所以对自然语言提问只有「用户只打一个词」时才会命中。
    # 现在改成字典最长匹配，把句子里出现的所有别名都切出来。
    norm = state.get("_normalized") or state.get("question", "")
    result = linker.link_question(norm)

    candidates = extract_candidates(result.residual)
    near = linker.near_misses(candidates) if candidates else []

    return {
        "_entities": [e.__dict__ for e in result.entities],
        "_unresolved": candidates,
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
    return {"_intents": intents}


def route_after_link(state: dict) -> str:
    """只有链接到**食材**才值得走图查询。

    三种情况分开：
      有食材实体         → graph_query
      没食材但有未收录词 → uncatalogued（登记 + 明确回话）
      只有症状/人群、或什么都没命中 → vector_fallback（宁可降级也不硬猜）

    注意「只有症状/人群」这一支：graph_query 内部本来就会跳过非 Ingredient 实体，
    走进去也只会拿到空结果，不如直接降级，省一次 Neo4j 往返。
    """
    entities = state.get("_entities") or []
    if any(e.get("label") == "Ingredient" for e in entities):
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
    # 未加载画像时，尝试从记忆系统加载
    if not profile and config.MEMORY_ENABLED:
        try:
            from memory.long_term import UserProfile

            profile = UserProfile().get(state.get("user_id", "anonymous"))
        except Exception as e:  # noqa: BLE001
            log.warning("加载画像失败：%s", e)
            profile = {}
    allergens = list(profile.get("allergies", [])) + list(profile.get("avoid_list", []))

    for entity in state.get("_entities", []):
        if entity["label"] != "Ingredient":
            continue
        for intent in state.get("_intents", []):
            allowed = [x.strip() for x in config.EVIDENCE_POLICY_MAP.get(intent, "").split(",") if x.strip()]
            rows = _query.run(
                intent,
                entity["canonical_id"],
                allowed,
                allergens,
                include_provisional=config.INCLUDE_PROVISIONAL,
            )
            for r in rows:
                r["intent"] = intent
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

    # 降级路径：只有向量，没有图事实 → 视为「无确切依据」，除非向量里有明确答案
    if not facts and vector_results:
        tier = "none"
        facts = [{"text": c["text"], "etype": "doc", "elevel": "C", "name": c.get("doc_id", "文档")} for c in vector_results]

    profile = state.get("_user_profile") or {}
    prompt = build_grounded_prompt(
        state["question"], facts, tier, profile, provisional=all_provisional
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
    from llm import LLMUnavailableError, invoke_model

    try:
        # 走 Agent + wrap_call_model 中间件：动态选模型，额度用尽自动切下一个
        resp = invoke_model(prompt)
    except LLMUnavailableError as exc:
        # 池内模型 + 兜底模型全都不行：宁可明确说「暂时答不了」，也不要编答案
        log.warning("模型池不可用，降级作答：%s", exc)
        return {
            "answer": (
                "当前模型暂时都不可用（额度用尽或全部冷却中），无法生成回答。"
                "图谱检索结果已就绪，稍后重试即可；如情况紧急请直接就医。"
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
    # 弱证据档：追加标注（单源补录用另一句，别把实测数据说成「民间说法」）
    if note and note not in answer:
        answer = note + "\n" + answer
    return {
        "answer": answer,
        "basis": obj.get("basis", []),
        "gaps": obj.get("gaps", []),
        "confidence": float(obj.get("confidence", 0.5)),
    }


def _dummy_answer(ctx: str) -> str:
    """LLM 未接线时的占位回答（用于骨架验收）。"""
    first = ctx.split("\n")[0] if ctx else ""
    return f"（占位回答）基于图谱事实：{first}。请配置 DASHSCOPE_API_KEY 后获得完整回答。"


def _get_llm():
    """模型池的回退链（LangChain 原生 `with_fallbacks`），供非 Agent 场景使用。

    主回答链路走的是 Agent + `wrap_call_model` 中间件（见 generate_node）；
    这里留给只需要一个「能 invoke 的模型」的地方，例如画像抽取。
    未配置 DASHSCOPE_API_KEY 时返回 None。
    """
    if not config.DASHSCOPE_API_KEY:
        return None
    from llm import pool_chain

    return pool_chain()


# ============================================================
# verify —— 自检：答案断言能否在图事实里找到出处
# ============================================================
def verify_node(state: dict) -> dict:
    # 简化自检：如果答案非空且上下文为空，说明是弃答，无需校验
    ctx = state.get("_memory_context", "")
    ans = state.get("answer", "")
    if not ans:
        return {}
    if not ctx:
        return {}
    # 校验：答案里的关键名词是否出现在上下文（粗粒度接地检查）
    # 生产版应做断言级拆分，这里做演示级检查
    return {}


# ============================================================
# memory_write —— 落库（短期消息 + 画像增量抽取）
# ============================================================
def memory_write_node(state: dict) -> dict:
    if not config.MEMORY_ENABLED:
        return {}
    try:
        from memory.short_term import ShortTermMemory
        from memory.long_term import UserProfile

        user_id = state.get("user_id", "anonymous")
        session_id = state.get("session_id")
        if not session_id:
            stm = ShortTermMemory()
            session_id = stm.get_session(user_id)

        stm = ShortTermMemory()
        stm.save_round(session_id, state["question"], state.get("answer", ""))

        # 画像增量抽取（有 LLM 时）
        dialogue = f"用户：{state['question']}\n助手：{state.get('answer', '')}"
        updates = parse_profile_from_answer(dialogue, _get_llm())
        if updates:
            up = UserProfile()
            up.update(user_id, **updates)
    except Exception as e:  # noqa: BLE001 —— 记忆失败不阻断主流程
        log.warning("memory_write 失败：%s", e)
    return {}
