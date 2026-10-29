"""
kg/request.py —— 未收录食材的登记（Phase 9.1）

只做「登记」，不做抓取。抓取是 9.2 的事，放在离线/后台，不在这条路径上。

三条出口，顺序不能乱：

  1. 不是食材        → 直接挡掉（阿司匹林、痛风、来首诗）
  2. 疑似别名漏收    → 写 dict_alias_candidate，反问用户确认（比建新节点便宜得多）
  3. 真是新食材      → 写 kg_ingredient_request（幂等 UPSERT，累加 hit_count）

为什么第 2 步必须在第 3 步之前：
  「黑大豆 / 黑豆粥」这种只是多了个字的说法，如果先按新食材登记，
  就会给同一个东西开三条抓取任务，既烧钱又给图谱埋下重复节点的隐患 ——
  正好是 「西红柿 / 番茄不能建成两个节点」要避免的情况。

中文模糊匹配不用 pg_trgm：实测 similarity('黑大豆','黑豆') = 0.000，
中文走 kg/textsim 的字级相似度（包含关系 + 编辑距离）。
"""
from __future__ import annotations

import logging
import re
from dataclasses import dataclass, field
from typing import Any

import psycopg

import config
from kg.normalize import AliasStore, normalize_text

import db

log = logging.getLogger("kg.request")

# ============================================================
# 词表
# ============================================================
# 烹饪方式 / 形态后缀：黑豆粥 → 黑豆
_SUFFIXES = [
    "沙拉", "凉拌", "清炒", "红烧", "水煮", "干煸",
    "粥", "汤", "茶", "汁", "粉", "糊", "羹", "膏",
    "丝", "片", "块", "泥", "酱", "末", "丁", "馅",
]
_PREFIXES = ["凉拌", "清炒", "红烧", "水煮", "干煸", "炒", "煮", "蒸", "炖", "烤", "炸", "生"]

# 分词用的停顿词：这些词出现在候选片段里就切开
# 容易漏的几个是实测踩出来的：
#   缺「配」→「红烧肉配什么好」会切出「红烧肉配」，剥掉「红烧」后剩「肉配」被登记
#   缺「合适 / 好处 / 做法」→ 会切出「XX合适」这类伪食材
_BREAK_WORDS = [
    "能不能", "可以", "适合", "合适", "怎么", "什么", "如何", "多少", "多久",
    "是不是", "有没有", "为什么", "哪些", "哪个", "好处", "坏处", "做法",
    "推荐", "建议", "一起", "同食", "搭配", "请问", "我想", "想要",
    "还有", "以及", "比较", "有点", "一下", "这个", "那个", "吃了",
    "配", "搭",
    "吗", "呢", "吧", "啊", "呀", "的", "和", "与", "跟", "同", "及", "或",
    "能", "可", "会", "要", "想", "吃", "喝", "用", "做", "好", "最", "更",
    "含", "有", "是", "高", "低", "多", "少", "不", "太",
]

# 明显不是食材的名词（营养素 / 指标 / 泛称）——不挡的话「嘌呤」「热量」会被当成食物去抓
_NON_FOOD_TERMS = {
    "嘌呤", "热量", "卡路里", "营养", "营养成分", "维生素", "蛋白质", "脂肪", "碳水",
    "碳水化合物", "膳食纤维", "纤维", "糖分", "含糖量", "胆固醇", "血糖", "血脂",
    "血压", "尿酸", "功效", "作用", "禁忌", "副作用", "做法", "食谱", "价格", "热量表",
    "食物", "食材", "东西", "水果", "蔬菜", "肉类", "主食", "零食", "饮料",
}

_TERM_RE = re.compile(r"^[\u4e00-\u9fa5A-Za-z]{2,8}$")
_RESIDUAL_SEP = "\x00"

# 只保留"强停顿词"做剥离后的二次校验。
# 单字虚词（好/是/多/不…）不能进这个集合：会误伤「多宝鱼」「无花果」这类正常食材名，
# 结果就是剥离全部变成空操作，归一形同虚设。
_STRONG_BREAK = [w for w in _BREAK_WORDS if len(w) >= 2] + ["配", "搭"]


# ============================================================
# 结果
# ============================================================
@dataclass
class RegisterResult:
    action: str                      # new_request | duplicated | alias_candidate
                                     # | catalogued | rejected_not_food
                                     # | rejected_not_ingredient | rate_limited | not_ready
    food_key: str = ""
    term: str = ""
    method: str = ""                 # 归一方式：asis / strip_suffix:粥 / strip_prefix:炒
    request_id: int | None = None
    status: str = ""
    hit_count: int = 0
    suggestion: dict[str, Any] | None = None
    message: str = ""
    code: str = ""
    extra: dict[str, Any] = field(default_factory=dict)

    @property
    def registered(self) -> bool:
        """是否产生了新的登记（决定要不要告诉用户「已提交」）。"""
        return self.action in ("new_request", "duplicated")


# ============================================================
# 文本处理
# ============================================================
def is_plausible_food(term: str) -> bool:
    """粗筛：长度 2-8、纯中英文字符、不是营养素/泛称。"""
    if not term or not _TERM_RE.match(term):
        return False
    if term in _NON_FOOD_TERMS:
        return False
    return True


def normalize_food_term(raw: str) -> tuple[str, str]:
    """剥掉烹饪/形态前后缀，得到中心词。返回 (中心词, 归一方式)。

    最后一道保险：剥完如果还残留停顿词（说明前面分段没切干净），
    说明这次剥离不可信，退回原词 —— 宁可登记得粗一点，也不要造出「肉配」这种伪食材。
    """
    term = normalize_text(raw)
    if not term:
        return "", "empty"

    for suffix in sorted(_SUFFIXES, key=len, reverse=True):
        if term.endswith(suffix) and len(term) - len(suffix) >= 2:
            center = term[: -len(suffix)]
            if any(w in center for w in _STRONG_BREAK):
                return term, "asis"
            return center, f"strip_suffix:{suffix}"
    for prefix in sorted(_PREFIXES, key=len, reverse=True):
        if term.startswith(prefix) and len(term) - len(prefix) >= 2:
            center = term[len(prefix):]
            if any(w in center for w in _STRONG_BREAK):
                return term, "asis"
            return center, f"strip_prefix:{prefix}"
    return term, "asis"


def extract_candidates(residual: str, limit: int = 3) -> list[str]:
    """从「没有被任何别名占用的残余片段」里抽候选词。

    residual 里用 \\x00 标记已被已知别名占掉的位置，所以先按它切段，
    再用停顿词切，最后按顺序取前 limit 个通过粗筛的词。
    """
    if not residual:
        return []

    pieces: list[str] = []
    for segment in residual.split(_RESIDUAL_SEP):
        text = segment
        for word in sorted(_BREAK_WORDS, key=len, reverse=True):
            text = text.replace(word, " ")
        # 标点、数字、其他符号一律当分隔
        text = re.sub(r"[^\u4e00-\u9fa5A-Za-z]+", " ", text)
        pieces.extend(text.split())

    out: list[str] = []
    for piece in pieces:
        piece = piece.strip()
        if not is_plausible_food(piece):
            continue
        if piece in out:
            continue
        out.append(piece)
        if len(out) >= limit:
            break
    return out


# ============================================================
# 登记
# ============================================================
def _count_today(conn, where: str, params: tuple) -> int:
    cur = conn.cursor()
    cur.execute(
        f"SELECT count(*) FROM kg_ingredient_request WHERE {where} "
        f"AND created_at > now() - interval '1 day'",
        params,
    )
    row = cur.fetchone()
    return int(row[0]) if row else 0


def register(
    raw: str,
    who: str = "anonymous",
    source: str = "chat",
    near_misses: list[dict] | None = None,
    skip_user_limit: bool = False,
    count_hit: bool = True,
) -> RegisterResult:
    """把一次「未收录」请求落到正确的出口。任何异常都不能让 /chat 500。

    count_hit 为 False 时不把这次写入算进需求热度：新行 hit_count 为 0，
    已有行不改 hit_count、原话和最近提及。管理员手工登记传 False。
    """
    if not config.FOOD_REQUEST_ENABLED:
        return RegisterResult(
            action="rejected_not_food", code="disabled",
            message="知识库暂未收录该食材。",
        )

    term, method = normalize_food_term(raw)
    if not is_plausible_food(term):
        return RegisterResult(
            action="rejected_not_food", term=term, method=method, code="not_food",
            message="没能识别出具体是哪种食材，换一个说法试试（比如「黑豆」）？",
        )

    store = AliasStore(config.PG_DSN)
    try:
        # ---- 已经收录？----
        exact = store.exact_lookup(term)
        if any(h["entity_type"] == "Ingredient" for h in exact):
            return RegisterResult(
                action="catalogued", term=term, method=method, food_key=term,
                message=f"「{term}」其实已经在知识库里了。",
            )
        # ---- 是人群 / 症状 / 禁忌，不是食材 ----
        # 一个词可能同时挂在多个类型上（种子里「痛风」既是 Symptom 又是 痛风患者 Population），
        # 谁排前面原来取决于 SELECT 返回顺序 —— 用户看到的提示会随机变。
        # 这里按固定优先级排，保证同一句话永远给同一个回答。
        others = [h for h in exact if h["entity_type"] != "Ingredient"]
        if others:
            others.sort(key=lambda h: _non_ingredient_rank(h["entity_type"]))
            return RegisterResult(
                action="rejected_not_ingredient", term=term, method=method,
                code="not_ingredient",
                message=(
                    f"「{term}」是{_label_cn(others[0]['entity_type'])}，不是食材 —— "
                    f"你想问的是某道菜 / 某种食物能不能吃吗？"
                ),
                extra={"entity_type": others[0]["entity_type"],
                       "canonical_id": others[0]["canonical_id"]},
            )

        # ---- 疑似别名漏收：先反问，不建新条目 ----
        cands = near_misses if near_misses is not None else store.fuzzy_lookup(
            term, min_sim=config.FOOD_NEAR_MISS_MIN_SIM
        )
        best = cands[0] if cands else None
        if best:
            cid = best.get("candidate_id") or best.get("canonical_id")
            label = best.get("candidate_type") or best.get("entity_type")
            alias = best.get("candidate_alias") or best.get("alias")
            if label == "Ingredient":
                store.add_candidate(
                    entity_type="Ingredient", alias=term, alias_norm=term,
                    canonical_id=cid, note=f"sim={best.get('score') or best.get('sim')}",
                )
                return RegisterResult(
                    action="alias_candidate", term=term, method=method, food_key=term,
                    suggestion={"canonical_id": cid, "alias": alias,
                                "score": best.get("score") or best.get("sim")},
                    message=(
                        f"「{term}」没有直接收录，但你说的可能是「{alias}」？"
                        f"如果是同一种东西的新叫法，我已经记下来，确认后会合并；"
                        f"如果不是，告诉我我再单独登记。"
                    ),
                )

        # ---- 限流 ----
        with db.connect() as conn:
            exists = conn.execute(
                "SELECT id, status, hit_count FROM kg_ingredient_request WHERE food_key = %s",
                (term,),
            ).fetchone()

            if exists is None:
                # 管理员登记传 skip_user_limit，不占这个人的每日额度。全站上限仍生效。
                if (
                    not skip_user_limit
                    and _count_today(conn, "requested_by = %s", (who,))
                    >= config.FOOD_REQUEST_DAILY_LIMIT_PER_USER
                ):
                    return RegisterResult(
                        action="rate_limited", term=term, method=method, food_key=term,
                        code="user_daily_limit",
                        message="今天提交的新食材有点多了，明天再继续吧。",
                    )
                if _count_today(conn, "true", ()) >= config.FOOD_REQUEST_GLOBAL_DAILY_LIMIT:
                    log.warning("全站未收录登记达到日上限，本次不写库：%s", term)
                    return RegisterResult(
                        action="rate_limited", term=term, method=method, food_key=term,
                        code="global_daily_limit",
                        message="今天登记的量已满，已记录到日志，稍后会补上。",
                    )

            # ---- 幂等 UPSERT ----
            # count_hit 为 False（手工登记）时新行从 0 起；
            # 撞上已有食物名则次数、原话、最近提及都保持原样。
            initial_hits = 1 if count_hit else 0
            hit_delta = 1 if count_hit else 0
            row = conn.execute(
                """
                INSERT INTO kg_ingredient_request
                    (food_key, raw_mention, normalized, requested_by, source, status, hit_count)
                VALUES (%s, %s, %s, %s, %s, 'queued', %s)
                ON CONFLICT (food_key) DO UPDATE SET
                    hit_count         = kg_ingredient_request.hit_count + %s,
                    last_requested_at = CASE WHEN %s THEN now()
                                        ELSE kg_ingredient_request.last_requested_at END,
                    raw_mention       = CASE WHEN %s THEN EXCLUDED.raw_mention
                                        ELSE kg_ingredient_request.raw_mention END
                RETURNING id, status, hit_count
                """,
                (
                    term, normalize_text(raw), term, who, source,
                    initial_hits, hit_delta, count_hit, count_hit,
                ),
            ).fetchone()

            request_id, status, hit_count = row
            # 排队去抓：同一 food_key 只建一个任务，重复登记不会重复消耗预算
            if status == "queued":
                conn.execute(
                    "INSERT INTO kg_fetch_task (request_id, food_key) VALUES (%s, %s) "
                    "ON CONFLICT (food_key) DO NOTHING",
                    (request_id, term),
                )
        if status == "rejected":
            return RegisterResult(
                action="duplicated", term=term, method=method, food_key=term,
                request_id=request_id, status=status, hit_count=hit_count,
                message=(
                    f"「{term}」之前提交过，但经核实没能收录进知识库，"
                    f"就不重复登记了。"
                ),
            )

        return RegisterResult(
            action="new_request" if hit_count <= 1 else "duplicated",
            term=term, method=method, food_key=term,
            request_id=request_id, status=status, hit_count=hit_count,
            message=(
                f"暂未收录「{term}」。已记录并提交给知识库管理员（编号 #{request_id}），"
                f"审核通过后就能查到。在那之前我不会给出它的营养数据或搭配建议，"
                f"避免给你不准确的信息。"
            ),
        )

    except psycopg.errors.UndefinedTable:
        log.error("kg_ingredient_request 不存在，请先运行 python -m scripts.migrate")
        return RegisterResult(
            action="not_ready", term=term, method=method, code="table_missing",
            message=f"暂未收录「{term}」，登记功能还没就绪（数据库未迁移）。",
        )
    except Exception as exc:  # noqa: BLE001 —— 登记失败绝不能让 /chat 崩
        log.exception("登记未收录食材失败：%s", exc)
        return RegisterResult(
            action="not_ready", term=term, method=method, code="error",
            message=f"暂未收录「{term}」。",
        )


def _label_cn(entity_type: str) -> str:
    return {
        "Population": "人群 / 疾病",
        "Symptom": "症状",
        "Taboo": "禁忌对象",
        "Nutrient": "营养素",
    }.get(entity_type, "非食材概念")


# 同一词挂多个类型时的提示优先级。
# 人群/疾病排最前：用户问「痛风能吃吗」最需要听到的是「痛风是疾病不是食材」，
# 而不是「痛风是个症状」——后者听起来像在挑字眼。
_NON_INGREDIENT_PRIORITY = ("Population", "Taboo", "Nutrient", "Symptom")


def _non_ingredient_rank(entity_type: str) -> tuple[int, str]:
    try:
        return (_NON_INGREDIENT_PRIORITY.index(entity_type), "")
    except ValueError:
        return (len(_NON_INGREDIENT_PRIORITY), entity_type)


# ============================================================
# 查询（给 API 用）
# ============================================================
def get_request(request_id: int, who: str) -> dict[str, Any] | None:
    """按 id 查登记状态。只返回属于当前身份的条目（管理员另走 /admin）。"""
    with db.connect() as conn:
        row = conn.execute(
            "SELECT id, food_key, raw_mention, status, hit_count, created_at, last_requested_at "
            "FROM kg_ingredient_request WHERE id = %s AND requested_by = %s",
            (request_id, who),
        ).fetchone()
    if row is None:
        return None
    return {
        "request_id": row[0],
        "food_key": row[1],
        "raw_mention": row[2],
        "status": row[3],
        "status_text": _status_cn(row[3]),
        "hit_count": row[4],
        "created_at": row[5].isoformat() if row[5] else None,
        "last_requested_at": row[6].isoformat() if row[6] else None,
    }


def _status_cn(status: str) -> str:
    return {
        "queued": "已登记，等待处理",
        "fetching": "正在从权威来源获取",
        "fetched": "已获取，等待校验",
        "pending_review": "等待人工审核",
        "approved": "已收录",
        "rejected": "未通过核实",
    }.get(status, status)
