"""
kg/normalize.py —— 归一与实体链接

核心思路：归一是数据问题，不是 if-else 问题。
  - 所有别名统一收进 PostgreSQL 的 dict_alias 表（canonical_id 为核心键）
  - 未命中的口语词落 dict_alias_candidate 表，人工确认后回流

实体链接：**字典最长匹配** + 模糊兜底 + 向量兜底
  1. 字典最长匹配：把问题里出现的别名全部切出来（「痛风能吃海鲜吗」→ 痛风/海鲜）
     —— 这一级才是主力。旧实现拿整句去 exact_lookup，而整句永远不会等于某个别名，
        所以对自然语言提问 100% 失效，只有用户只打一个词时才命中。
  2. 模糊匹配：中文用 kg/textsim 的字级相似度（pg_trgm 对中文实测全是 0.000）
  3. 向量：Milvus kg_entity（可选，未接线时自然跳过）
  4. LLM 消歧：留给后续

未命中时不是简单扔掉，而是：
  - residual（没被任何别名占用的片段）交给 kg.request 抽候选词
  - 对候选词再做一次模糊匹配，得到 near_misses（「你是不是想问黑豆？」）
"""
from __future__ import annotations

import logging
import re
import unicodedata
from dataclasses import dataclass, field
from typing import Any

import config
from kg.textsim import char_similarity

import db

log = logging.getLogger("kg.normalize")

_alias_store: "AliasStore | None" = None


def get_alias_store() -> "AliasStore":
    """进程内一份别名表。改完 dict_alias 后要重启，或把 `_alias_store` 置空。

    问答链路以前每次请求 new 一个 AliasStore，缓存跟着请求丢掉，
    全表别名每问都重读。测试会在每条用例前清掉这份缓存。
    """
    global _alias_store
    if _alias_store is None:
        _alias_store = AliasStore(config.PG_DSN)
    return _alias_store

# 模糊匹配的可用区间：低于这个值基本是两回事
NEAR_MISS_MIN_SIM = 0.62


# ============================================================
# 文本归一（去空白 / 全半角 / 繁简依赖 opencc，此处做基础处理）
# ============================================================
def normalize_text(s: str) -> str:
    """基础归一：去首尾空白、全角转半角、统一分隔符。"""
    if not s:
        return ""
    s = unicodedata.normalize("NFKC", s).strip()
    s = re.sub(r"\s+", "", s)
    return s


@dataclass
class LinkedEntity:
    """实体链接结果。"""
    canonical_id: str
    label: str
    name: str
    score: float
    method: str  # substring | exact | fuzzy | vector | llm
    matched_alias: str = ""


@dataclass
class LinkResult:
    entities: list[LinkedEntity] = field(default_factory=list)
    unresolved: list[str] = field(default_factory=list)
    residual: str = ""                                  # 没被任何别名占用的原文片段
    near_misses: list[dict[str, Any]] = field(default_factory=list)

    @property
    def ingredient_entities(self) -> list[LinkedEntity]:
        return [e for e in self.entities if e.label == "Ingredient"]


# ============================================================
# 别名表访问（PostgreSQL）
# ============================================================
class AliasStore:
    """dict_alias 表的读写封装。"""

    def __init__(self, dsn: str):
        # dsn 只保留兼容：连接一律走 db.connect()（池按 config.PG_DSN 建）。
        # 传别的库不会生效 —— 需要连别的库就别复用这个类。
        if dsn != config.PG_DSN:
            log.warning(
                "AliasStore 收到与 config.PG_DSN 不同的 DSN，仍会连 config.PG_DSN"
            )
        self.dsn = dsn
        self._cache: list[dict[str, Any]] | None = None

    def exact_lookup(self, alias_norm: str, entity_type: str | None = None) -> list[dict]:
        with db.connect() as conn:
            cur = conn.cursor()
            if entity_type:
                cur.execute(
                    "SELECT entity_type, canonical_id, alias, confidence FROM dict_alias "
                    "WHERE alias_norm = %s AND entity_type = %s",
                    (alias_norm, entity_type),
                )
            else:
                cur.execute(
                    "SELECT entity_type, canonical_id, alias, confidence FROM dict_alias "
                    "WHERE alias_norm = %s",
                    (alias_norm,),
                )
            rows = cur.fetchall()
        return [
            {"entity_type": r[0], "canonical_id": r[1], "alias": r[2], "confidence": r[3]}
            for r in rows
        ]

    def all_aliases(self, refresh: bool = False) -> list[dict]:
        """全量别名（进程内缓存）。字典规模几百到几千条，一次读进来比反复查库快得多。

        改完 dict_alias 后需重启进程或 refresh=True 才生效 ——
        别名回流本来就是「人工确认后」的低频动作，接受这个代价。
        """
        if self._cache is not None and not refresh:
            return self._cache
        with db.connect() as conn:
            cur = conn.cursor()
            cur.execute(
                "SELECT entity_type, canonical_id, alias, alias_norm, confidence FROM dict_alias"
            )
            self._cache = [
                {
                    "entity_type": r[0],
                    "canonical_id": r[1],
                    "alias": r[2],
                    "alias_norm": r[3],
                    "confidence": r[4],
                }
                for r in cur.fetchall()
            ]
        return self._cache

    def trigram_lookup(self, alias: str, entity_type: str | None = None, limit: int = 5) -> list[dict]:
        """pg_trgm 模糊匹配。

        ⚠️ 对中文实测无效（黑大豆 vs 黑豆 = 0.000），中文请用 fuzzy_lookup。
        保留它是给英文名 / 拼音这类拉丁串用的。
        """
        with db.connect() as conn:
            cur = conn.cursor()
            if entity_type:
                cur.execute(
                    "SELECT entity_type, canonical_id, alias, similarity(alias_norm, %s) AS sim "
                    "FROM dict_alias WHERE entity_type = %s AND similarity(alias_norm, %s) > 0.6 "
                    "ORDER BY sim DESC LIMIT %s",
                    (alias, entity_type, alias, limit),
                )
            else:
                cur.execute(
                    "SELECT entity_type, canonical_id, alias, similarity(alias_norm, %s) AS sim "
                    "FROM dict_alias WHERE similarity(alias_norm, %s) > 0.6 "
                    "ORDER BY sim DESC LIMIT %s",
                    (alias, alias, limit),
                )
            rows = cur.fetchall()
        return [
            {"entity_type": r[0], "canonical_id": r[1], "alias": r[2], "sim": r[3]}
            for r in rows
        ]

    def fuzzy_lookup(
        self,
        term: str,
        entity_type: str | None = None,
        min_sim: float = NEAR_MISS_MIN_SIM,
        limit: int = 3,
    ) -> list[dict]:
        """字级相似度匹配（中文主力）。返回按相似度倒序的候选。"""
        scored = []
        for row in self.all_aliases():
            if entity_type and row["entity_type"] != entity_type:
                continue
            sim = char_similarity(term, row["alias_norm"])
            if sim >= min_sim:
                scored.append({**row, "sim": round(sim, 4)})
        scored.sort(key=lambda r: -r["sim"])
        return scored[:limit]

    def add_candidate(
        self,
        entity_type: str,
        alias: str,
        alias_norm: str,
        canonical_id: str | None,
        note: str = "",
    ) -> None:
        """未命中的口语词落候选表，人工确认后回流。

        冲突时**只在 pending 状态下更新建议**，已审核过的（approved/rejected）一律不动：
          - 图谱是会变的。今天「黑大豆」最像「黑豆」，明天补录了一个真·黑大豆，
            再问一次就该指向新节点；用 DO NOTHING 会让第一条建议永久生效。
          - 但已经人工确认过的结论不能被后来的启发式覆盖 —— 那是审核意见，不是缓存。
        """
        with db.connect() as conn:
            conn.execute(
                "INSERT INTO dict_alias_candidate "
                "(entity_type, alias, alias_norm, suggested_canonical_id) "
                "VALUES (%s, %s, %s, %s) "
                "ON CONFLICT (entity_type, alias_norm) DO UPDATE "
                "SET suggested_canonical_id = EXCLUDED.suggested_canonical_id "
                "WHERE dict_alias_candidate.status = 'pending' "
                "  AND dict_alias_candidate.suggested_canonical_id "
                "      IS DISTINCT FROM EXCLUDED.suggested_canonical_id",
                (entity_type, alias, alias_norm, canonical_id),
            )
        if note:
            log.info("别名候选已记录：%s → %s（%s）", alias, canonical_id or "待定", note)


# ============================================================
# 实体链接器
# ============================================================
class EntityLinker:
    """字典最长匹配 + 模糊 + 向量（可选）。"""

    def __init__(self, alias_store: AliasStore, vector_search=None, llm=None):
        self.alias = alias_store
        self.vector_search = vector_search  # fn(query, top_k) -> list[dict]
        self.llm = llm  # 消歧用的 LLM 客户端

    # ---------- 整句提问 → 实体（主力入口）----------
    def link_question(self, question: str) -> LinkResult:
        """把一句话里出现的所有已知别名切出来，返回未被覆盖的残余片段。

        最长匹配优先：先按别名长度倒序，长别名先占位，
        避免「凉拌黑木耳」被「木耳」这种短别名抢先匹配掉半截。
        """
        norm = normalize_text(question)
        result = LinkResult()
        if not norm:
            return result

        occupied = [False] * len(norm)
        for row in sorted(
            self.alias.all_aliases(), key=lambda r: -len(r["alias_norm"] or "")
        ):
            alias = row["alias_norm"]
            if not alias:
                continue
            start = 0
            while True:
                idx = norm.find(alias, start)
                if idx < 0:
                    break
                end = idx + len(alias)
                if any(occupied[idx:end]):
                    start = idx + 1
                    continue
                for k in range(idx, end):
                    occupied[k] = True
                result.entities.append(
                    LinkedEntity(
                        canonical_id=row["canonical_id"],
                        label=row["entity_type"],
                        name=alias,
                        score=float(row["confidence"] or 1.0),
                        method="substring",
                        matched_alias=row["alias"],
                    )
                )
                start = end

        # 去重：同一 canonical_id 在同一句里出现多次只留第一次
        seen: set[tuple[str, str]] = set()
        deduped = []
        for e in result.entities:
            key = (e.label, e.canonical_id)
            if key in seen:
                continue
            seen.add(key)
            deduped.append(e)
        result.entities = deduped

        result.residual = "".join(
            ch if not occupied[i] else "\x00" for i, ch in enumerate(norm)
        )
        return result

    # ---------- 单个提及 → 实体（旧接口，保留）----------
    def link(self, mention: str, entity_type: str | None = None) -> LinkResult:
        norm = normalize_text(mention)
        result = LinkResult(residual=norm)

        exact = self.alias.exact_lookup(norm, entity_type)
        if exact:
            for e in exact:
                result.entities.append(
                    LinkedEntity(
                        canonical_id=e["canonical_id"],
                        label=e["entity_type"],
                        name=mention,
                        score=e.get("confidence", 1.0),
                        method="exact",
                        matched_alias=e["alias"],
                    )
                )
            return result

        # 中文模糊（字级相似度）
        fuzzy = self.alias.fuzzy_lookup(norm, entity_type, min_sim=0.88)
        if fuzzy:
            best = fuzzy[0]
            result.entities.append(
                LinkedEntity(
                    canonical_id=best["canonical_id"],
                    label=best["entity_type"],
                    name=mention,
                    score=best["sim"],
                    method="fuzzy",
                    matched_alias=best["alias"],
                )
            )
            return result

        if self.vector_search is not None:
            vec = self.vector_search(norm, top_k=3)
            if vec and vec[0]["score"] >= config.ENTITY_LINK_THRESHOLD:
                best = vec[0]
                result.entities.append(
                    LinkedEntity(
                        canonical_id=best["canonical_id"],
                        label=best.get("label", entity_type or ""),
                        name=mention,
                        score=best["score"],
                        method="vector",
                        matched_alias=mention,
                    )
                )
                return result

        result.unresolved.append(mention)
        result.near_misses = self.near_misses([mention])
        return result

    # ---------- 近似候选（而不是直接判「未收录」）----------
    def near_misses(self, terms: list[str], min_sim: float = NEAR_MISS_MIN_SIM) -> list[dict]:
        out: list[dict] = []
        for term in terms:
            for cand in self.alias.fuzzy_lookup(term, min_sim=min_sim, limit=2):
                out.append(
                    {
                        "mention": term,
                        "candidate_id": cand["canonical_id"],
                        "candidate_type": cand["entity_type"],
                        "candidate_alias": cand["alias"],
                        "score": cand["sim"],
                    }
                )
        return out
