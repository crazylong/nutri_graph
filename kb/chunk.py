"""把解析块收成可入库的块：标题回填、长文本切开、同一篇里相同图片只留一张。

切块分两级，对齐参考项目 Multimodal_RAG 的「标题 + 语义」思路：
1. 标题级：parse 阶段已按 #/##/### 切出 heading_path，这里保持边界不跨标题合并。
2. 语义级：超长文本块按句子嵌入、找相邻句子相似度的低谷作为断点切开，
   取代原来一刀切的字符递归切分（RecursiveCharacterTextSplitter）。

语义切分复用 embed_mm 的多模态嵌入通道，不引入 langchain-experimental。
语义嵌入不可用时降级回字符切分，保证发布不因语义切分失败而中断。

问答类（doc_kind=qa）另走一条路：按「问 / 答」成对切，一问答一块。
问答文档的价值在问题与答案的对应关系，语义切分会把答案从问题下面劈开，
检索时问题就匹配不上答案了，所以这类文档不做语义切分。
"""
from __future__ import annotations

import hashlib
import logging
import re

from langchain_text_splitters import RecursiveCharacterTextSplitter

import config

log = logging.getLogger("kb.chunk")

# 句子切分：中文按句末标点，英文按句号，兼顾换行。
_SENT_SPLIT = re.compile(r"(?<=[。！？；!?;])\s*|\n+")

# 问答块的问题行：Q:/Q1./问：/问题1、等写法，必须独占行首。
_QA_START = re.compile(r"(?m)^[ \t]*(?:问题|问|Q|q)[ \t]*\d*[ \t]*[:：.、)）][ \t]*")

# 语义切分的余弦相似度阈值：相邻句子相似度低于该值即视为语义断点。
# 取值偏保守（宁可多连一点，也不把语义完整的一段切碎）。
_SIM_BREAK = 0.55


def prepare(blocks: list[dict], doc_kind: str = "") -> list[dict]:
    """入口：保持原签名与输出字段不变；doc_kind 只影响切分策略。"""
    splitter = RecursiveCharacterTextSplitter(
        chunk_size=config.KB_CHUNK_CHARS,
        chunk_overlap=config.KB_CHUNK_OVERLAP,
        length_function=len,
    )
    qa = doc_kind == "qa"
    last_heading = ""
    seen_md5: set[str] = set()
    out: list[dict] = []
    for block in blocks:
        heading = (block.get("heading_path") or "").strip() or last_heading
        if heading:
            last_heading = heading
        if block["block_kind"] == "image":
            raw = block.get("image_bytes")
            if not raw:
                name = block.get("image_name") or "image"
                note = (block.get("text") or "").strip() or f"图:{name}"
                out.append({**block, "block_kind": "text", "text": note, "heading_path": heading, "image_bytes": None})
                continue
            digest = hashlib.md5(raw).hexdigest()
            if digest in seen_md5:
                continue
            seen_md5.add(digest)
            label = (block.get("text") or "").strip() or f"图:{block.get('image_name') or digest[:8]}"
            out.append({**block, "text": label, "heading_path": heading, "image_md5": digest})
            continue
        body = (block.get("text") or "").strip()
        if not body:
            continue
        pieces = _split_qa(body, heading, splitter) if qa else _split_body(body, splitter)
        for piece in pieces:
            piece = piece.strip()
            if piece:
                out.append({**block, "text": piece, "heading_path": heading, "image_bytes": None})
    return out


def _split_qa(body: str, heading: str, splitter: RecursiveCharacterTextSplitter) -> list[str]:
    """问答类切分：一个问答一块，问题和答案不分离。

    三种形态都照顾到：
    1. 正文里有 `Q:` / `问：` / `问题1、` 行首标记 → 按标记成对切；
    2. 没有标记，但所在标题本身是个问句（如 `## 怎么报销？`）→ 把标题补成问题行；
    3. 两者都不是 → 退回普通切分，不做臆测。
    """
    pairs = _qa_pairs(body)
    if not pairs:
        leaf = heading.rsplit(" / ", 1)[-1].strip() if heading else ""
        if leaf.endswith(("？", "?")):
            return [f"问：{leaf}\n{body}"]
        return _split_body(body, splitter)
    out: list[str] = []
    for pair in pairs:
        if len(pair) <= config.KB_CHUNK_CHARS:
            out.append(pair)
        else:
            out.extend(_split_qa_pair(pair, splitter))
    return out


def _qa_pairs(body: str) -> list[str]:
    """按问题行起点把正文切成若干段，每段是一个问答；首段若无问题标记则原样保留。"""
    starts = [m.start() for m in _QA_START.finditer(body)]
    if not starts:
        return []
    out: list[str] = []
    head = body[: starts[0]].strip()
    if head:
        out.append(head)
    for i, start in enumerate(starts):
        end = starts[i + 1] if i + 1 < len(starts) else len(body)
        pair = body[start:end].strip()
        if pair:
            out.append(pair)
    return out


def _split_qa_pair(pair: str, splitter: RecursiveCharacterTextSplitter) -> list[str]:
    """超长问答：问题行留在每一块前面，只切答案。"""
    question, _, answer = pair.partition("\n")
    answer = answer.strip()
    if not answer:
        return splitter.split_text(pair)
    return [f"{question.strip()}\n{piece}" for piece in splitter.split_text(answer)]


def _split_body(body: str, splitter: RecursiveCharacterTextSplitter) -> list[str]:
    """长文本切分：语义切分优先，失败降级到字符切分。"""
    if not config.KB_SEMANTIC_CHUNK:
        return splitter.split_text(body)
    if len(body) <= config.KB_CHUNK_CHARS:
        return [body]
    try:
        pieces = _semantic_split(body)
        if pieces:
            return pieces
    except Exception as exc:  # noqa: BLE001 —— 语义切分失败不阻断发布
        log.warning("语义切分失败，降级字符切分：%s", exc)
    return splitter.split_text(body)


def _semantic_split(body: str) -> list[str]:
    """按句子嵌入的相邻余弦相似度找语义断点。

    流程：句子切分 → 批量嵌入 → 算相邻相似度 → 相似度低于阈值处切块，
    再把每个语义段按字符上限兜底（避免单段过长）。
    """
    sents = [s.strip() for s in _SENT_SPLIT.split(body) if s.strip()]
    if len(sents) < 2:
        return [body]
    vectors = _embed_sentences(sents)
    if not vectors:
        return []

    # 断点：相邻两句相似度低于阈值，说明语义在这里转折。
    breaks: set[int] = {0}
    for i in range(1, len(sents)):
        if _cosine(vectors[i - 1], vectors[i]) < _SIM_BREAK:
            breaks.add(i)
    breaks.add(len(sents))

    # 按断点聚合，再按字符上限兜底。
    pieces: list[str] = []
    buf: list[str] = []
    buf_len = 0
    for i in range(len(sents)):
        buf.append(sents[i])
        buf_len += len(sents[i])
        at_break = (i + 1) in breaks
        over_limit = buf_len >= config.KB_CHUNK_CHARS
        if at_break or over_limit:
            pieces.append("".join(buf))
            buf = []
            buf_len = 0
    if buf:
        pieces.append("".join(buf))
    return [p for p in pieces if p.strip()]


def _embed_sentences(sents: list[str]) -> list[list[float]] | None:
    """批量嵌入句子，返回与句子等长的向量列表；失败返回 None。"""
    from kb import embed_mm

    vectors: list[list[float]] = []
    for s in sents:
        try:
            vectors.append(embed_mm.embed_text(s))
        except Exception as exc:  # noqa: BLE001
            log.warning("语义切分嵌入失败（第 %d 句），中止语义切分：%s", len(vectors), exc)
            return None
    return vectors


def _cosine(a: list[float], b: list[float]) -> float:
    if not a or not b or len(a) != len(b):
        return 0.0
    dot = sum(x * y for x, y in zip(a, b))
    na = sum(x * x for x in a) ** 0.5
    nb = sum(y * y for y in b) ** 0.5
    if na == 0 or nb == 0:
        return 0.0
    return dot / (na * nb)


__all__ = ["prepare"]
