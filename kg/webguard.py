"""
kg/webguard.py —— 网页回答专有的两样东西

政治类、疗效类、营销类走 kg.sensitive，这里不存那些词。
广告法绝对化（最好/最佳）也留在 sensitive_word 里，只给图谱的
rewrite_answer 用。网页裁剪带上「最好」会把「最好先焯水」整句删掉。
"""
from __future__ import annotations

import re

from kg.sensitive import Hit, scan, scan_political

# 健康建议型绝对化。不进 sensitive_word：图谱回答里「痛风患者不宜」是合法注意事项。
ABSOLUTE_WORDS = (
    "不宜", "禁用", "忌食", "必吃", "一定要",
    "绝对不能", "千万不要", "长期服用",
)

# 药材。扫原问题，不扫切出来的词：补停顿词之前「天麻」会粘在「天麻煲汤」上。
HERBS = (
    "天麻", "当归", "黄芪", "党参", "人参", "西洋参", "灵芝", "杜仲",
    "山茱萸", "铁皮石斛", "西红花", "三七", "何首乌", "甘草",
)

# 和 rewrite_answer 同一句式。没有句末标点时整段算一句，命中就整段删，不截半句。
_SENT_SPLIT = re.compile(r"(?<=[。！？；!?;\n])")
_SENTENCE_CATEGORIES = frozenset({"medical_claim", "marketing"})


def is_herbal(question: str) -> bool:
    text = question or ""
    return any(herb in text for herb in HERBS)


def has_political(text: str) -> list[Hit]:
    return scan_political(text or "")


def trim_sentences(text: str) -> tuple[str, list[Hit]]:
    """按句丢掉疗效类、营销类和健康建议型绝对化。剩一句就留，多短都留。

    不处理政治类，也不处理 superlative。政治类由调用方整条丢掉。
    """
    hits: list[Hit] = []
    kept: list[str] = []
    for sent in _SENT_SPLIT.split(text or ""):
        if not sent.strip():
            continue
        matched = [
            hit for hit in scan(sent, field_name="snippet")
            if hit.category in _SENTENCE_CATEGORIES
        ]
        absolute = next((word for word in ABSOLUTE_WORDS if word in sent), "")
        if absolute:
            matched.append(Hit(
                field="snippet",
                word=absolute,
                category="absolute",
                action="drop",
                severity=2,
                raw_action="drop",
            ))
        if matched:
            hits.extend(matched)
            continue
        kept.append(sent)
    return "".join(kept).strip(), hits
