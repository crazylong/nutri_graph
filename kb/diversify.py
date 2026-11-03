"""文本提问的近义收敛。截断由调用方决定条数，精排不在这里。"""
from __future__ import annotations

import re
import unicodedata

_NUMBER = re.compile(r"\d+(?:\.\d+)?")


def diversify(hits: list[dict], *, limit: int, threshold: float) -> list[dict]:
    """按传入顺序收。数字多重集不同的不合并。凑不满 limit 不把丢掉的补回。"""
    size = max(1, int(limit))
    kept: list[dict] = []
    seen: list[tuple[set[str], list[str]]] = []
    for hit in hits or []:
        plain = _plain(hit.get("text"))
        grams = _bigrams(plain)
        numbers = _numbers(plain)
        if any(_same(grams, numbers, old_grams, old_numbers, threshold) for old_grams, old_numbers in seen):
            continue
        kept.append(hit)
        seen.append((grams, numbers))
        if len(kept) >= size:
            break
    return kept


def _plain(text: str | None) -> str:
    folded = unicodedata.normalize("NFKC", text or "")
    compact = re.sub(r"\s+", "", folded).strip()
    return compact or "图"


def _bigrams(text: str) -> set[str]:
    if len(text) < 2:
        return {text}
    return {text[i : i + 2] for i in range(len(text) - 1)}


def _numbers(text: str) -> list[str]:
    return sorted(_NUMBER.findall(text))


def _same(grams: set[str], numbers: list[str], old_grams: set[str], old_numbers: list[str], threshold: float) -> bool:
    if numbers != old_numbers:
        return False
    if not grams or not old_grams:
        return False
    overlap = len(grams & old_grams)
    score = overlap / ((len(grams) * len(old_grams)) ** 0.5)
    return score >= threshold
