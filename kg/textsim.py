"""
kg/textsim.py —— 中文短词的相似度

为什么不继续用 pg_trgm：
  实测 `similarity()` 在中文短词上全是 0.000 ——
    黑大豆 vs 黑豆 → 0.000
    黑豆粥 vs 黑豆 → 0.000
    西红柿子 vs 西红柿 → 0.000
  pg_trgm 的三元组在中文（多字节）上基本不产生公共片段，
  所以 `AliasStore.trigram_lookup` 对中文是死代码。英文名 / 拼音场景它仍然有效，
  那边继续用 trigram，中文走这里。

两个信号，按可靠性排序：
  1. 包含关系：黑大豆 ⊃ 黑豆、凉拌黑木耳 ⊃ 黑木耳 —— 中文里最常见的「多了个字」形态
  2. 字级编辑距离：西红柿子 vs 西红柿 = 0.75
包含关系排前面，因为编辑距离算不出「长度差 1 但语义相同」的直觉权重。
"""
from __future__ import annotations


def levenshtein(a: str, b: str) -> int:
    """字级编辑距离（中文按字符算，不是字节）。"""
    if a == b:
        return 0
    if not a:
        return len(b)
    if not b:
        return len(a)
    prev = list(range(len(b) + 1))
    for i, ca in enumerate(a, 1):
        cur = [i]
        for j, cb in enumerate(b, 1):
            cur.append(min(
                prev[j] + 1,          # 删
                cur[j - 1] + 1,       # 增
                prev[j - 1] + (ca != cb),  # 改
            ))
        prev = cur
    return prev[-1]


def char_similarity(a: str, b: str) -> float:
    """0~1，1 表示完全相同。"""
    if not a or not b:
        return 0.0
    if a == b:
        return 1.0
    if a in b or b in a:
        # 包含关系：长度比越小说明差得越多（黑豆 ⊂ 黑豆粥 是 2/3）
        return min(len(a), len(b)) / max(len(a), len(b))
    dist = levenshtein(a, b)
    return max(0.0, 1.0 - dist / max(len(a), len(b)))


def best_match(term: str, candidates: list[tuple[str, object]]) -> tuple[float, object] | None:
    """在 [(alias_norm, payload)] 里找最相似的一条。candidates 通常几百条，直接线性扫。"""
    best: tuple[float, object] | None = None
    for name, payload in candidates:
        sim = char_similarity(term, name)
        if best is None or sim > best[0]:
            best = (sim, payload)
    return best
