"""
graph/boundary.py —— 生成边界控制

三档回答：
  有据      —— 至少 1 条 A/B 级 → 直接回答 + 引用
  部分有据  —— 只有 C/D 级 → 回答 + 显著标注「未经验证」
  无据      —— 图 0 命中且向量低分 → 固定话术 + 拒绝具体建议

同时负责：从回答中抽取用户画像增量（过敏/慢病），供记忆系统更新。
"""
from __future__ import annotations

import json
import re
from typing import Any

import config

# 三档判定
TIER_STRONG = "strong"       # 有 A/B 级证据
TIER_WEAK = "weak"           # 只有 C/D 级
TIER_NONE = "none"           # 无据


def classify_tier(evidence_counts: dict[str, int]) -> str:
    """根据证据等级分布判定回答档位。"""
    if not evidence_counts:
        return TIER_NONE
    if evidence_counts.get("A", 0) + evidence_counts.get("B", 0) > 0:
        return TIER_STRONG
    return TIER_WEAK


WEAK_DISCLAIMER = (
    "（提示：以下内容基于传统经验或民间说法，证据等级较低，未经充分验证，请谨慎参考。）"
)

# 单源补录数据的提示。和 WEAK_DISCLAIMER 分开写：
# 那些数据是 USDA / 中国食物成分表**实测**出来的，不是民间说法。
# 说成「传统经验」既不准确，也会让人低估可信度；真正的问题是没做交叉验证。
PROVISIONAL_DISCLAIMER = (
    "（提示：该食材数据为单一来源补录，已通过人工审核但未做多源交叉验证，"
    "营养数值仅供参考。）"
)

NONE_ANSWER = "抱歉，我没有找到关于这个问题的确切依据，无法给出具体的食疗建议。"


def build_grounded_prompt(
    question: str,
    facts: list[dict[str, Any]],
    tier: str,
    profile: dict[str, Any] | None = None,
    provisional: bool = False,
    history: str = "",
) -> str:
    """构造严格接地 prompt。

    provisional=True 表示这些事实全是**单一来源的实测数据**（如 USDA / 中国食物成分表），
    已通过人工审核但没有多源交叉验证。这时不能套用 TIER_WEAK 那句
    「传统经验或民间说法」——那是另一回事，会把实测数据说贬，模型也会照抄成错话。
    """
    lines = []
    for f in facts:
        name = f.get("name") or f.get("cid")
        etype = f.get("etype", "")
        elevel = f.get("elevel", "")
        claim = f.get("claim", "") or f.get("text", "")
        lines.append(f"[{etype}/{elevel}] {name}：{claim}")
    ctx = "\n".join(lines)

    allergy_note = ""
    if profile and (profile.get("allergies") or profile.get("avoid_list")):
        allergy_note = f"\n用户已知过敏/忌口：{profile.get('allergies', []) + profile.get('avoid_list', [])}。所有建议必须排除这些。"

    tier_rule = {
        TIER_STRONG: "存在权威依据（A/B 级），可以正常回答，并在结尾列出依据。",
        TIER_WEAK: "只有传统经验或民间说法（C/D 级），回答时必须明确标注「未经验证」，不得当作确定结论。",
        TIER_NONE: "没有任何图谱事实，只能回答「没有找到确切依据」，禁止给出任何具体建议。",
    }[tier]

    if tier == TIER_WEAK and provisional:
        tier_rule = (
            "以上是单一来源的实测数据（已通过人工审核，但没有第二个来源交叉验证）。"
            "可以给出具体数值，但必须说明「数据为单一来源、未做交叉验证、仅供参考」；"
            "禁止把这些数据描述成「传统经验」「民间说法」或「未经证实的传言」——"
            "它们是实测值，问题在于验证程度，不在于证据类型。"
        )

    history_block = f"近期对话（只用于理解指代，不能当成图谱事实）：\n{history}\n\n" if history else ""

    return (
        "你是食疗养生助手，只能依据下面给出的图谱事实回答，禁止补充任何图外知识或常识推断。\n"
        f"{tier_rule}\n"
        f"{allergy_note}\n\n"
        f"{history_block}"
        f"图检索事实：\n{ctx}\n\n"
        f"用户问题：{question}\n\n"
        '请以 JSON 返回：{"answer":"...","basis":["引用的关键事实"],"gaps":["未覆盖的点"],"confidence":0.0-1.0}'
    )


def build_document_prompt(
    question: str,
    chunks: list[dict[str, Any]],
    profile: dict[str, Any] | None = None,
    history: str = "",
) -> str:
    """图谱 0 命中、向量高于阈值时用。片段必须标明是文档，不是图谱关系。"""
    lines = []
    for c in chunks:
        text = c.get("text") or ""
        doc_id = c.get("doc_id") or "文档"
        lines.append(f"[文档/{doc_id}] {text}")
    ctx = "\n".join(lines)
    allergy_note = ""
    if profile and (profile.get("allergies") or profile.get("avoid_list")):
        allergy_note = (
            f"用户已知过敏/忌口：{profile.get('allergies', []) + profile.get('avoid_list', [])}。"
            "转述时排除这些。\n"
        )
    history_block = f"近期对话（只用于理解指代）：\n{history}\n\n" if history else ""
    return (
        "你是食疗养生助手。下面是向量检索到的文档片段，不是知识图谱里的关系事实。\n"
        "只能转述片段里写明的内容，并在回答里说明「来自文档检索，不是图谱结论」。\n"
        "片段里没有直接答案时，回答「没有找到确切依据」，禁止用常识补充。\n"
        f"{allergy_note}\n"
        f"{history_block}"
        f"文档片段：\n{ctx}\n\n"
        f"用户问题：{question}\n\n"
        '请以 JSON 返回：{"answer":"...","basis":["引用的片段"],"gaps":["未覆盖的点"],"confidence":0.0-1.0}'
    )


# 用户原话里出现这些，才可能是在透露自己的饮食约束。
# 没有就不要再打一次模型：搭配、营养这类问法占了绝大多数请求。
_PROFILE_CUES = (
    "过敏", "忌口", "忌食", "不吃", "不能吃",
    "痛风", "糖尿", "高血压", "低血压", "肾病",
    "孕妇", "怀孕", "哺乳", "慢病", "慢性",
)


def mentions_health_constraint(text: str) -> bool:
    """这句话里有没有可能在说自己的过敏、慢病或忌口。"""
    return any(cue in (text or "") for cue in _PROFILE_CUES)


# 画像抽取 prompt（after_model 钩子用）
PROFILE_EXTRACT_PROMPT = (
    "从下面这轮对话中判断用户是否透露了饮食相关的健康约束。\n"
    "仅当用户明确表达时抽取，禁止推断。\n"
    "输出 JSON：{\"allergies\":[], \"conditions\":[], \"avoid_list\":[]}\n"
    "allergies=食物过敏；conditions=慢性病（如痛风、糖尿病、高血压）；avoid_list=主动忌口/不吃的东西。\n\n"
    f"对话：{'{dialogue}'}"
)


def parse_profile_from_answer(dialogue: str, llm) -> dict[str, list[str]]:
    """从对话抽取用户画像增量（需 llm 客户端）。"""
    if llm is None:
        return {}
    prompt = PROFILE_EXTRACT_PROMPT.replace("{dialogue}", dialogue)
    resp = llm.invoke(prompt)
    content = getattr(resp, "content", resp)
    m = re.search(r"\{.*\}", content, re.DOTALL)
    if not m:
        return {}
    try:
        obj = json.loads(m.group(0))
    except json.JSONDecodeError:
        return {}
    return {k: v for k, v in obj.items() if isinstance(v, list)}
