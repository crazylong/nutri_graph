"""基于已入库的 eval 租户 chunk，用 LLM 自动生成知识库评测样本。

产出两个文件：
  eval/kb_qa.jsonl    每行 {"question","reference","tenant_id"}
  eval/kb_pairs.jsonl 每行 {"question","expected_id"}

用法（在服务器上，先跑 scripts.import_testdata 导入文档）：
  python -m scripts.gen_kb_samples --tenant eval --limit 20
  python -m scripts.gen_kb_samples --tenant eval --doc-id <id> --limit 2 --append

- 每个文本 chunk 调一次 LLM，生成「一个问题 + 标准答案」，问题要能用该 chunk 原文回答。
- expected_id 用真实 chunk_id（来自 kb_chunk 表），供 Recall@K 对照。
- --limit 是有效题的条数，正文太短的块跳过并继续找，不是先切前 N 块再生成。
- --doc-id 可重复。--append 只追加 kb_pairs.jsonl，不改 kb_qa.jsonl，问题重复的跳过。
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
TENANT = "eval"

sys.path.insert(0, str(ROOT))

import db  # noqa: E402
from llm import primary_model  # noqa: E402

OUT_QA = ROOT / "eval" / "kb_qa.jsonl"
OUT_PAIRS = ROOT / "eval" / "kb_pairs.jsonl"

_PROMPT = """你是一名知识库评测题生成器。下面是一段从企业文档里切出的正文块（可能带章节路径）。

请根据这段内容生成一道**能用该正文直接回答**的问题，并给出标准答案。

要求：
1. 问题要简短具体（不超过 25 个字），能被这段正文回答，不要问正文里没有的内容。
2. 标准答案要忠于正文，用自己的话概括，不超过 60 个字，不要照抄。
3. 只输出一行 JSON，不要有任何额外文字，格式：{{"question": "...", "reference": "..."}}

正文块：
[章节] {heading}
{text}
"""

# 弃答题：问一些知识库里肯定没有的内容，验证系统是否诚实拒答。
_ABSTAIN_QUESTIONS = [
    "公司年终奖的发放标准是什么？",
    "员工离职后股票期权如何处理？",
    "公司食堂有哪些菜品？",
]


def _load_chunks(tenant: str, doc_ids: list[str] | None = None) -> list[dict]:
    # 摘要行的 block_kind 也是 text。image_path 为空才是正文，避免拿摘要出题。
    where = "d.tenant_id = %s AND c.block_kind = 'text' AND c.image_path IS NULL"
    args: list = [tenant]
    if doc_ids:
        where += " AND d.id::text = ANY(%s)"
        args.append(doc_ids)
    with db.connect() as conn:
        rows = conn.execute(
            f"""
            SELECT c.id, c.text, c.heading_path
            FROM kb_chunk c
            JOIN kb_document d ON d.id = c.doc_id
            WHERE {where}
            ORDER BY d.updated_at, c.page_no, c.id
            """,
            args,
        ).fetchall()
    return [{"chunk_id": r[0], "text": r[1], "heading": r[2] or ""} for r in rows]


def merge_pairs(existing: list[dict], new_rows: list[dict]) -> tuple[list[dict], int]:
    """按问题去重。重复的不写入，返回跳过条数。"""
    seen = {(row.get("question") or "").strip() for row in existing}
    skipped = 0
    merged = list(existing)
    for row in new_rows:
        question = (row.get("question") or "").strip()
        if question in seen:
            skipped += 1
            continue
        seen.add(question)
        merged.append(row)
    return merged, skipped


def _gen_one(chunk: dict, model) -> dict | None:
    text = (chunk["text"] or "").strip()
    if len(text) < 20:
        return None
    prompt = _PROMPT.format(heading=chunk["heading"], text=text[:2000])
    try:
        msg = model.invoke(prompt)
        content = (msg.content or "").strip()
    except Exception as exc:  # noqa: BLE001
        print(f"  生成失败（chunk {chunk['chunk_id'][:8]}）：{exc}")
        return None
    # 抽取 JSON（LLM 可能带 ```json 围栏或前后废话）
    content = content.strip().strip("`")
    if content.startswith("json"):
        content = content[4:].strip()
    try:
        obj = json.loads(content)
    except json.JSONDecodeError:
        # 退而求其次：找第一个 { 到最后一个 }
        start = content.find("{")
        end = content.rfind("}")
        if start == -1 or end == -1:
            print(f"  无法解析 JSON（chunk {chunk['chunk_id'][:8]}）：{content[:80]!r}")
            return None
        try:
            obj = json.loads(content[start:end + 1])
        except json.JSONDecodeError:
            return None
    question = (obj.get("question") or "").strip()
    reference = (obj.get("reference") or "").strip()
    if not question or not reference:
        return None
    return {"question": question, "reference": reference}


def _read_jsonl(path: Path) -> list[dict]:
    if not path.is_file():
        return []
    rows = []
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if line:
            rows.append(json.loads(line))
    return rows


def _write_jsonl(path: Path, rows: list[dict]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    text = "\n".join(json.dumps(row, ensure_ascii=False) for row in rows)
    path.write_text((text + "\n") if text else "", encoding="utf-8")


def main(argv: list[str] | None = None) -> None:
    p = argparse.ArgumentParser()
    p.add_argument("--tenant", default=TENANT)
    p.add_argument("--limit", type=int, default=20, help="有效题条数，正文太短的块不计数")
    p.add_argument("--doc-id", action="append", default=[], help="只从这些文档出题，可重复")
    p.add_argument("--append", action="store_true", help="追加到 kb_pairs.jsonl，不改 kb_qa.jsonl")
    args = p.parse_args(argv)

    chunks = _load_chunks(args.tenant, args.doc_id or None)
    print(f"可选文本块 {len(chunks)} 个，生成 {args.limit} 条有效题")
    if not chunks:
        print("没有可用的块，请先跑 scripts.import_testdata")
        return

    qa_rows = []
    pairs_rows = []
    model = primary_model(max_tokens=1024, enable_thinking=False)
    for chunk in chunks:
        if len(pairs_rows) >= args.limit:
            break
        item = _gen_one(chunk, model)
        if not item:
            continue
        qa_rows.append({
            "question": item["question"],
            "reference": item["reference"],
            "tenant_id": args.tenant,
        })
        pairs_rows.append({
            "question": item["question"],
            "expected_id": chunk["chunk_id"],
        })
        print(f"  [{len(pairs_rows)}] {item['question'][:40]}")

    if args.append:
        merged, skipped = merge_pairs(_read_jsonl(OUT_PAIRS), pairs_rows)
        _write_jsonl(OUT_PAIRS, merged)
        if skipped:
            print(f"跳过重复问题 {skipped} 条")
        print(f"\n追加完成：kb_pairs.jsonl {len(merged)} 条（本次新题 {len(pairs_rows) - skipped}）")
        print(f"  {OUT_PAIRS}")
        return

    # 追加弃答题（reference=ABSTAIN），验证系统是否诚实拒答。不进 pairs（无 expected_id）。
    for q in _ABSTAIN_QUESTIONS:
        qa_rows.append({"question": q, "reference": "ABSTAIN", "tenant_id": args.tenant})
        print(f"  [弃答] {q}")

    _write_jsonl(OUT_QA, qa_rows)
    _write_jsonl(OUT_PAIRS, pairs_rows)
    print(f"\n生成完成：kb_qa.jsonl {len(qa_rows)} 条，kb_pairs.jsonl {len(pairs_rows)} 条")
    print(f"  {OUT_QA}")
    print(f"  {OUT_PAIRS}")


if __name__ == "__main__":
    main()
