"""
kg/sensitive.py —— 敏感词扫描（词表在库里，代码只留兜底）

设计上刻意分两层，别混：

1. **词的默认动作存在库里**（`sensitive_word.action`），改词不用发版。
2. **实际动作按字段折算**（`_effective_action`）：
   - `political`：任何字段都是 block，且不进审核队列（不进库就不留传播面）
   - 名称类字段（名称/别名/英文名/学名）：任何类别都按 block ——
     一个食材名里出现"功效""治疗"，它本来就不是食材名
   - 描述类字段（备注/来源/原始描述）：只拦**疗效断言**，"功效"二字放行

归一化做三件事，缺一件就能被绕过去：
   NFKC（全角转半角）→ 简繁折叠 → 去掉所有非文字字符（治·疗 → 治疗、加 微 信 → 加微信）

**简繁折叠只覆盖词表涉及的常用字。** 全量转换要 opencc，它不在 requirements 里，
也没打算为一个词表引入 —— 需要更全的转换时，把变体当独立词条写进词表更直接。
"""
from __future__ import annotations

import hashlib
import logging
import re
import unicodedata
from dataclasses import dataclass
from typing import Iterable

log = logging.getLogger("kg.sensitive")

# 名称类：窄口径，命中即挡
NAME_FIELDS = ("name", "aliases", "english_name", "scientific_name")
# 描述类：宽口径，medical_claim 只拦断言不拦"功效"二字
DESC_FIELDS = ("note", "source", "source_desc")

# 各对象的字段顺序。键是对象里的键名，值是给用户看的列名。
# 按对象分开写是因为同一个语义在不同对象里键名不一样：
#   importer 的 payload 用 source，kg_food_bundle 的列叫 source_desc
IMPORT_FIELDS = (
    ("name", "名称"),
    ("aliases", "别名"),
    ("english_name", "英文名"),
    ("scientific_name", "学名"),
    ("source", "来源"),
    ("note", "备注"),
)
BUNDLE_FIELDS = (
    ("canonical_name", "名称"),
    ("aliases", "别名"),
    ("scientific_name", "学名"),
    ("source_desc", "原始描述"),
    ("note", "备注"),
    ("source", "来源"),
)

# 输入侧命中政治类后的固定话术。
# 不说"暂未收录"：那会让用户以为库里迟早会收录，接着反复问，
# 反而把敏感词刷进登记表。也不说"无法识别"，太像系统报错。
POLITICAL_ANSWER = "这个词我没法当作食材来回答。换个具体的食材或搭配问题试试？比如「黑豆和什么一起吃好」。"
# 疗效断言被剔干净之后的兜底（不能说"我不知道"，要给下一步）
MEDICAL_FALLBACK = "这个我只能按图谱里收录的食材信息来回答，疗效和治病方面给不了建议。"

REDACT = "[内容已过滤]"


class SensitiveBlocked(Exception):
    """批准动作被敏感词闸拦下。调用方各自映射：
    kg.promote → PromoteError（api/admin 已映射 409）；api.admin → HTTPException 409；
    kg.review CLI → 打印错误并不动状态。
    """

    def __init__(self, hits: list["Hit"], confirm_missing: bool = False):
        self.hits = hits
        self.blocking = [h for h in hits if h.action == "block"]
        self.pending = [h for h in hits if h.action == "review"]
        self.confirm_missing = confirm_missing
        if self.blocking:
            cats = "/".join(sorted({h.category for h in self.blocking}))
            msg = f"命中敏感词（{cats}），不允许批准"
        else:
            words = "/".join(sorted({h.word for h in self.pending}))[:60]
            msg = f"命中待人工确认的敏感词（{words}），需带 confirm_sensitive=true 再提交"
        super().__init__(msg)


@dataclass
class Hit:
    field: str
    word: str
    category: str
    action: str          # 折算后的实际动作
    severity: int = 2
    raw_action: str = ""  # 库里记的默认动作，留着做审计

    def as_dict(self) -> dict:
        return {
            "field": self.field,
            "word": self.word,
            "category": self.category,
            "action": self.action,
            "severity": self.severity,
        }


# ------------------------------------------------------------
# 归一化
# ------------------------------------------------------------
# 只覆盖词表会用到的字。全量转换需要 opencc（不在依赖里），
# 更全的做法是把变体当独立词条写进 sensitive_word。
_T2S = {
    "療": "疗", "癒": "愈", "處": "处", "診": "诊", "斷": "断", "藥": "药",
    "獨": "独", "臺": "台", "賴": "赖", "輪": "轮", "銷": "销", "郵": "邮",
    "國": "国", "級": "级", "對": "对", "華": "华", "電": "电", "話": "话",
    "聯": "联", "係": "系", "絡": "络", "網": "网", "車": "车", "醫": "医",
    "師": "师", "證": "证", "專": "专", "業": "业", "減": "减", "壓": "压",
    "血": "血", "糖": "糖", "脂": "脂", "癌": "癌", "癥": "症", "癱": "瘫",
    "療": "疗", "補": "补", "虛": "虚", "調": "调", "氣": "气", "濕": "湿",
    "熱": "热", "寒": "寒", "涼": "凉",
}


def normalize(s: str) -> str:
    """NFKC + 简繁折叠 + 去符号/空白。治·疗 → 治疗，加 微 信 → 加微信。"""
    if not s:
        return ""
    s = unicodedata.normalize("NFKC", s)
    s = "".join(_T2S.get(ch, ch) for ch in s)
    # \W 在 Python3 下是 Unicode 感知的：汉字算 \w，标点和空白不算
    return re.sub(r"\W", "", s, flags=re.UNICODE)


# ------------------------------------------------------------
# 词表
# ------------------------------------------------------------
# 内置兜底：库读不到时仍然生效。只放 block 级（政治 + 疗效断言）。
_BUILTIN: tuple[tuple[str, str], ...] = (
    ("台独", "political"),
    ("法轮功", "political"),
    ("达赖", "political"),
    ("治疗", "medical_claim"),
    ("治愈", "medical_claim"),
    ("根治", "medical_claim"),
    ("处方", "medical_claim"),
    ("诊断", "medical_claim"),
    ("疗效", "medical_claim"),
    ("药到病除", "medical_claim"),
    ("降三高", "medical_claim"),
    ("抗癌", "medical_claim"),
)

_words: list[tuple[str, str, str, str, int]] | None = None
_word_version: int = 1


def refresh() -> None:
    """词表改完调一下。进程内缓存，不按请求读库。"""
    global _words
    _words = None


def _load() -> list[tuple[str, str, str, str, int]]:
    global _words, _word_version
    if _words is not None:
        return _words
    try:
        import db

        with db.connect() as conn:
            rows = conn.execute(
                "SELECT word, category, action, severity FROM sensitive_word WHERE enabled"
            ).fetchall()
            v = conn.execute(
                "SELECT COALESCE(max(version), 1) FROM sensitive_word_version"
            ).fetchone()
        _words = [(normalize(w), w, c, a, s) for w, c, a, s in rows]
        _word_version = int(v[0]) if v else 1
    except Exception as exc:  # noqa: BLE001 —— 词表读不到不能让 /chat 挂掉
        log.warning("敏感词表读不到，回退内置兜底：%s: %s", type(exc).__name__, exc)
        _words = [(normalize(w), w, c, "block", 3) for w, c in _BUILTIN]
        _word_version = 0
    return _words


def current_version() -> int:
    _load()
    return _word_version


def _effective_action(category: str, default: str, field_name: str) -> str:
    if category == "political":
        return "block"
    if field_name in NAME_FIELDS or field_name in ("canonical_name",):
        return "block"
    return default


# ------------------------------------------------------------
# 扫描
# ------------------------------------------------------------
def scan(text: str, field_name: str = "") -> list[Hit]:
    """扫一段文本。field_name 决定折算后的动作。"""
    n = normalize(text)
    if not n:
        return []
    out: list[Hit] = []
    for nw, word, cat, act, sev in _load():
        if nw and nw in n:
            out.append(
                Hit(
                    field=field_name,
                    word=word,
                    category=cat,
                    action=_effective_action(cat, act, field_name),
                    severity=sev,
                    raw_action=act,
                )
            )
    return out


def scan_political(text: str) -> list[Hit]:
    """输入侧只扫政治类。疗效词出现在问句里是问法，不是词条 —— 交给 register 和输出扫描。"""
    return [h for h in scan(text, field_name="question") if h.category == "political"]


def scan_obj(obj: dict, fields: Iterable[tuple[str, str]] = IMPORT_FIELDS) -> list[Hit]:
    """按对象扫。键不存在就跳过，不报错 —— 各对象的字段本来就不齐。"""
    hits: list[Hit] = []
    for key, header in fields:
        val = obj.get(key)
        if val is None:
            continue
        text = "、".join(str(v) for v in val) if isinstance(val, (list, tuple)) else str(val)
        hits.extend(scan(text, field_name=key))
    return hits


def scan_edges(rows: Iterable[dict], text_key: str = "claim_text") -> list[Hit]:
    """扫边候选的断言原文。营养边那种"每 100g 含…"是程序拼的，风险低，
    但关系候选的 claim_text 是断言原文，必须扫。"""
    hits: list[Hit] = []
    for r in rows:
        hits.extend(scan(str(r.get(text_key) or ""), field_name=text_key))
    return hits


def hits_json(hits: Iterable[Hit]) -> list[dict]:
    return [h.as_dict() for h in hits]


# ------------------------------------------------------------
# 批准闸
# ------------------------------------------------------------
def assert_approvable(hits: Iterable[Hit], confirm: bool = False) -> None:
    """三个批准口共用的一道闸。命中 block → 拒绝；命中 review → 必须显式确认。

    为什么三个口都要过：页面上批准关系候选走的是 `api/admin.py::_set_status`
    （直接 UPDATE kg_candidate），而 `kg/merge` 消费 status='approved' ——
    只堵 approve_bundle 等于闸只关了一半。
    """
    hits = list(hits)
    blocking = [h for h in hits if h.action == "block"]
    if blocking:
        raise SensitiveBlocked(hits)
    pending = [h for h in hits if h.action == "review"]
    if pending and not confirm:
        raise SensitiveBlocked(hits, confirm_missing=True)


def assert_bundle_approvable(
    conn,
    bundle_id: int,
    obj: dict,
    *,
    confirm: bool = False,
    reviewer: str = "",
    fields: Iterable[tuple[str, str]] = BUNDLE_FIELDS,
) -> list[Hit]:
    """bundle 批准口的闸：扫 → 记审计 → 拦下或放行。

    `conn` 由调用方给：approve_bundle 里已经 `SELECT ... FOR UPDATE` 了，
    审计字段要写在同一个事务里。
    """
    from psycopg.types.json import Jsonb

    hits = scan_obj(obj, fields)
    log_hits("review", hits, source="approve", bundle_id=bundle_id)
    assert_approvable(hits, confirm=confirm)  # 拦下就在这里抛了

    try:
        conn.execute(
            "UPDATE kg_food_bundle SET sensitive_hits = %s, sensitive_reviewed_by = %s, "
            "     sensitive_reviewed_at = now(), sensitive_word_version = %s "
            "WHERE id = %s",
            (Jsonb(hits_json(hits)), reviewer, current_version(), bundle_id),
        )
    except Exception as exc:  # noqa: BLE001 —— 审计写不进去不该挡住批准流程本身
        log.warning("bundle %s 的敏感词审计字段写入失败：%s", bundle_id, exc)
    return hits


# ------------------------------------------------------------
# 输出扫描（步骤 2.9 / 计划 §3.7）
# ------------------------------------------------------------
def rewrite_answer(text: str) -> tuple[str, list[Hit]]:
    """按类别分两种动作，不是一律整段替换。

    - political：整段换成固定话术（出现在回答里就是事故，没有"保留部分内容"的选项）
    - medical_claim：保留食材事实，**按句剔除**含疗效断言的句子
      —— 整段替换会让"菠菜能治疗贫血吗"答不出菠菜，与完成条件冲突；
        不管又会留下疗效断言，同样冲突

    只扫模型生成的正文：固定免责声明含「诊断」（config._DEFAULT_DISCLAIMER），
    拼进来扫会让每条正常回答都命中。
    """
    if not text:
        return text, []
    hits = scan(text, field_name="answer")
    if not hits:
        return text, []

    if any(h.category == "political" for h in hits):
        return POLITICAL_ANSWER, hits

    kept = []
    for sent in re.split(r"(?<=[。！？；!?;\n])", text):
        if scan(sent, field_name="answer"):
            continue
        kept.append(sent)
    out = "".join(kept).strip()
    return (out or MEDICAL_FALLBACK), hits


# ------------------------------------------------------------
# 审计
# ------------------------------------------------------------
def term_hash(word: str) -> str:
    return hashlib.sha256(normalize(word).encode("utf-8")).hexdigest()[:16]


def log_hits(
    scene: str,
    hits: Iterable[Hit],
    *,
    source: str = "",
    bundle_id: int | None = None,
) -> None:
    """记命中。**不存原词**，只存 hash —— 日志会流转、会被备份。"""
    hits = list(hits)
    if not hits:
        return
    try:
        import db

        version = current_version()
        with db.connect() as conn:
            # psycopg3 的 Connection 没有 executemany，只有 cursor 有
            with conn.cursor() as cur:
                cur.executemany(
                    "INSERT INTO sensitive_hit_log "
                    "(scene, category, action, field, term_hash, source, bundle_id, "
                    " word_version) VALUES (%s,%s,%s,%s,%s,%s,%s,%s)",
                    [
                        (
                            scene, h.category, h.action, h.field,
                            term_hash(h.word), source, bundle_id, version,
                        )
                        for h in hits
                    ],
                )
    except Exception as exc:  # noqa: BLE001 —— 审计写不进去不能影响主流程
        log.warning("敏感词命中审计写入失败：%s: %s", type(exc).__name__, exc)
