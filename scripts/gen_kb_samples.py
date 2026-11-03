"""基于已入库的 eval 租户 chunk，用 LLM 自动生成知识库评测样本。

产出两个文件：
  eval/kb_qa.jsonl    每行 {"question","reference","tenant_id"}
  eval/kb_pairs.jsonl 每行 {"question","expected_id"}

用法（在服务器上，先跑 scripts.import_testdata 导入文档）：
  python -m scripts.gen_kb_samples --tenant eval --limit 20

- 每个文本 chunk 调一次 LLM，生成「一个问题 + 标准答案」，问题要能用该 chunk 原文回答。
- expected_id 用真实 chunk_id（来自 kb_chunk 表），供 Recall@K 对照。
- --limit 限制最多生成多少条（默认 20），避免一次烧太多 token。
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


def _load_chunks(tenant: str) -> list[dict]:
    with db.connect() as conn:
        rows = conn.execute(
            """
            SELECT c.id, c.text, c.heading_path
            FROM kb_chunk c
            JOIN kb_document d ON d.id = c.doc_id
            WHERE d.tenant_id = %s AND c.block_kind = 'text'
            ORDER BY d.updated_at, c.page_no, c.id
            """,
            (tenant,),
        ).fetchall()
    return [{"chunk_id": r[0], "text": r[1], "heading": r[2] or ""} for r in rows]


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


def main() -> None:
    p = argparse.ArgumentParser()
    p.add_argument("--tenant", default=TENANT)
    p.add_argument("--limit", type=int, default=20, help="最多生成多少条")
    args = p.parse_args()

    chunks = _load_chunks(args.tenant)
    print(f"eval 租户下共 {len(chunks)} 个文本块，最多生成 {args.limit} 条")
    if not chunks:
        print("没有可用的块，请先跑 scripts.import_testdata")
        return

    qa_rows = []
    pairs_rows = []
    model = primary_model(max_tokens=1024, enable_thinking=False)
    for chunk in chunks[: args.limit]:
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
        print(f"  [{len(qa_rows)}] {item['question'][:40]}")

    # 追加弃答题（reference=ABSTAIN），验证系统是否诚实拒答。不进 pairs（无 expected_id）。
    for q in _ABSTAIN_QUESTIONS:
        qa_rows.append({"question": q, "reference": "ABSTAIN", "tenant_id": args.tenant})
        print(f"  [弃答] {q}")

    OUT_QA.parent.mkdir(parents=True, exist_ok=True)
    OUT_QA.write_text(
        "\n".join(json.dumps(r, ensure_ascii=False) for r in qa_rows) + "\n",
        encoding="utf-8",
    )
    OUT_PAIRS.write_text(
        "\n".join(json.dumps(r, ensure_ascii=False) for r in pairs_rows) + "\n",
        encoding="utf-8",
    )
    print(f"\n生成完成：kb_qa.jsonl {len(qa_rows)} 条，kb_pairs.jsonl {len(pairs_rows)} 条")
    print(f"  {OUT_QA}")
    print(f"  {OUT_PAIRS}")


if __name__ == "__main__":
    main()
