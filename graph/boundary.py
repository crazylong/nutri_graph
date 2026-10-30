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

MODEL_UNAVAILABLE = (
    "当前模型暂时都不可用（额度用尽或全部冷却中），无法生成回答。"
    "检索结果已就绪，稍后重试即可；如情况紧急请直接就医。"
)


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

    allergy_note = profile_constraint_note(profile)

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
        "用纯文本回答，不要 JSON。"
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
    allergy_note = profile_constraint_note(profile, document=True)
    history_block = f"近期对话（只用于理解指代）：\n{history}\n\n" if history else ""
    return (
        "你是食疗养生助手。下面是向量检索到的文档片段，不是知识图谱里的关系事实。\n"
        "只能转述片段里写明的内容，并在回答里说明「来自文档检索，不是图谱结论」。\n"
        "片段里没有直接答案时，回答「没有找到确切依据」，禁止用常识补充。\n"
        f"{allergy_note}\n"
        f"{history_block}"
        f"文档片段：\n{ctx}\n\n"
        f"用户问题：{question}\n\n"
        "用纯文本回答，不要 JSON。"
    )


def build_web_prompt(
    question: str,
    snippets: list[dict[str, Any]],
    herbal: bool = False,
) -> str:
    """未收录食材的网页参考。不接收画像，避免把过敏和慢病交给第三方上下文。"""
    lines = []
    for snippet in snippets:
        site = snippet.get("site") or "网页"
        title = snippet.get("title") or ""
        content = snippet.get("content") or ""
        lines.append(f"[{site}] {title}\n{content}")
    herbal_note = ""
    if herbal:
        herbal_note = (
            "问题里出现了药材。不要给出剂量、疗程、功效，也不要下「适合什么人」的结论，"
            "只转述做法和片段里写明的注意事项。\n"
        )
    ctx = "\n".join(lines)
    return (
        "你是食疗养生助手。下面是公开网页片段，不是知识图谱里的事实。\n"
        "只能转述这些片段里写明的内容，禁止补充常识或图外知识。\n"
        "禁止给出功效、疗效、治疗、剂量、疗程或人群禁忌的确定结论。\n"
        "片段互相矛盾时，直接写「说法不一」，不要选边。\n"
        "不得声称这些内容经过审核、权威或来自知识库。\n"
        f"{herbal_note}"
        f"网页片段：\n{ctx}\n\n"
        f"用户问题：{question}\n\n"
        "用纯文本回答，不要 JSON。把握由系统记录，不超过 0.25。"
    )


# 用户原话里出现这些，才可能是在透露自己的情况。
# 没有就不要再打一次模型：搭配、营养这类问法占了绝大多数请求。
# 不用「男 / 女 / 喜欢」：会打中「男人补锌」「我喜欢吃番茄」，而且后句不一定是在说自己。
_PROFILE_CUES = (
    "过敏", "忌口", "忌食", "不吃", "不能吃",
    "痛风", "糖尿", "高血压", "低血压", "肾病",
    "孕妇", "怀孕", "哺乳", "慢病", "慢性",
    "岁", "身高", "体重", "公斤", "斤", "厘米",
    "失眠", "生理期", "月经", "爱好",
)

# 这些不是慢性病。模型若把它们放进 conditions，归到 symptoms。
_STATE_WORDS = ("失眠", "生理期", "月经", "怀孕", "孕期", "孕妇")

_GENDER_MAP = {
    "男": "男", "男性": "男", "男生": "男",
    "女": "女", "女性": "女", "女生": "女",
}

_LIST_FIELDS = ("allergies", "conditions", "avoid_list", "hobbies", "symptoms")


def mentions_health_constraint(text: str) -> bool:
    """这句话里有没有可能在说自己的过敏、慢病或忌口。"""
    return any(cue in (text or "") for cue in _PROFILE_CUES)


# 画像抽取 prompt（after_model 钩子用）
PROFILE_EXTRACT_PROMPT = (
    "从下面这轮对话中判断用户是否在说自己的情况。\n"
    "仅当用户明确在说自己时抽取，禁止推断。"
    "禁止把问句里的他人（如「男人补锌」「女生经期」）写成用户本人。\n"
    "输出 JSON："
    "{\"allergies\":[], \"conditions\":[], \"avoid_list\":[], "
    "\"hobbies\":[], \"symptoms\":[], "
    "\"age\":null, \"gender\":\"\", \"height_cm\":null, \"weight_kg\":null, \"weight_jin\":null}\n"
    "allergies=食物过敏；conditions=慢性病（痛风、糖尿病、高血压），"
    "不要把失眠、生理期、怀孕放进 conditions；"
    "avoid_list=主动忌口；hobbies=爱好；"
    "symptoms=症状或生理状态（失眠、生理期、怀孕）。\n"
    "age 为整数岁。gender 只填「男」或「女」。"
    "身高一律换算成厘米写入 height_cm。"
    "体重优先写入 weight_kg（公斤）。用户说的是斤时，把斤数写入 weight_jin，"
    "不要把斤数填进 weight_kg。一斤=0.5公斤。\n"
    "没有提到的字段用空数组、null 或空串，不要编造。\n\n"
    f"对话：{'{dialogue}'}"
)


def profile_constraint_note(profile: dict[str, Any] | None, *, document: bool = False) -> str:
    """过敏进提示词；人口学和症状只授权用来收窄建议，不能编造依据。

    爱好只落库，不放进回答提示词。
    """
    if not profile:
        return ""
    lines: list[str] = []
    blocked = list(profile.get("allergies") or []) + list(profile.get("avoid_list") or [])
    if blocked:
        verb = "转述时排除这些。" if document else "所有建议必须排除这些。"
        lines.append(f"用户已知过敏/忌口：{blocked}。{verb}")
    traits: list[str] = []
    if profile.get("age") not in (None, ""):
        traits.append(f"{profile['age']}岁")
    if profile.get("gender"):
        traits.append(str(profile["gender"]))
    if profile.get("height_cm") not in (None, ""):
        traits.append(f"身高{profile['height_cm']}厘米")
    if profile.get("weight_kg") not in (None, ""):
        traits.append(f"体重{profile['weight_kg']}公斤")
    if traits:
        lines.append("用户本人情况：" + "，".join(traits) + "。")
    symptoms = profile.get("symptoms") or []
    if symptoms:
        lines.append(f"用户提到的症状或生理状态：{symptoms}。")
    if not lines:
        return ""
    lines.append(
        "年龄、性别、身高、体重和症状只用来缩小建议范围，不能用来编造上面没有的关系或事实。"
    )
    return "\n" + "\n".join(lines) + "\n"


def normalize_profile_fields(obj: dict[str, Any]) -> dict[str, Any]:
    """把模型抽出的 JSON 收成可写入的字段。空值不出现在结果里。"""
    if not isinstance(obj, dict):
        return {}
    lists: dict[str, list[str]] = {}
    for key in _LIST_FIELDS:
        raw = obj.get(key)
        if not isinstance(raw, list):
            continue
        vals: list[str] = []
        for item in raw:
            if isinstance(item, str) and item.strip() and item.strip() not in vals:
                vals.append(item.strip())
        if vals:
            lists[key] = vals

    conditions = lists.get("conditions") or []
    symptoms = lists.get("symptoms") or []
    kept: list[str] = []
    for item in conditions:
        if any(word in item for word in _STATE_WORDS):
            if item not in symptoms:
                symptoms.append(item)
        else:
            kept.append(item)
    if kept:
        lists["conditions"] = kept
    elif "conditions" in lists:
        del lists["conditions"]
    if symptoms:
        lists["symptoms"] = symptoms

    out: dict[str, Any] = dict(lists)
    age = _as_int(obj.get("age"))
    if age is not None and 1 <= age <= 120:
        out["age"] = age
    gender = _GENDER_MAP.get(str(obj.get("gender") or "").strip())
    if gender:
        out["gender"] = gender
    height = _height_cm(obj)
    if height is not None:
        out["height_cm"] = height
    weight = _weight_kg(obj)
    if weight is not None:
        out["weight_kg"] = weight
    return out


def _as_number(value: Any) -> float | None:
    if isinstance(value, bool) or value is None or value == "":
        return None
    if isinstance(value, (int, float)):
        return float(value)
    if isinstance(value, str):
        text = value.strip()
        if not text:
            return None
        try:
            return float(text)
        except ValueError:
            return None
    return None


def _as_int(value: Any) -> int | None:
    if isinstance(value, str):
        value = value.strip().rstrip("岁")
    number = _as_number(value)
    if number is None or not number.is_integer():
        return None
    return int(number)


def _height_cm(obj: dict[str, Any]) -> float | None:
    cm = _as_number(obj.get("height_cm"))
    if cm is None:
        meters = _as_number(obj.get("height_m"))
        if meters is not None and meters > 0:
            cm = meters * 100
    if cm is None or cm <= 0:
        return None
    # 1.65 这种是米，不是厘米。正常身高不会小于 3 厘米。
    if cm < 3:
        cm *= 100
    if cm > 300:
        return None
    return round(cm, 1)


def _weight_kg(obj: dict[str, Any]) -> float | None:
    """已经换成公斤的用 weight_kg；只给了斤就按 1 斤 = 0.5 公斤换算。"""
    kg = _as_number(obj.get("weight_kg"))
    if kg is not None and kg > 0:
        return round(kg, 1)
    jin = _as_number(obj.get("weight_jin"))
    if jin is not None and jin > 0:
        return round(jin * 0.5, 1)
    return None


def parse_profile_from_answer(dialogue: str, llm) -> dict[str, Any]:
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
    return normalize_profile_fields(obj)
