"""企业知识库离线评估。只评有据回答的三项 Ragas，另算人工对照的 Recall@K。

用法（在服务器上，文档已经通过审核之后）：
  python -m eval.kb_run

eval/kb_pairs.jsonl 一行一对：{"question":"...","expected_id":"<审核时生成的 chunk_id>"}
eval/kb_qa.jsonl 一行一题：{"question":"...","reference":"..."}
租户从 expected_id 对应的 kb_chunk 读。通过之后不要再编辑或重新通过这些文档，块 id 会变。
"""
from __future__ import annotations

import json
import uuid
from pathlib import Path

from psycopg.types.json import Jsonb

import db
from kb.common import ABSTAIN, format_hit
from kb.search import search_question
from kb.workflow import run_ask

ROOT = Path(__file__).resolve().parent
PAIRS = ROOT / "kb_pairs.jsonl"
QA = ROOT / "kb_qa.jsonl"


def main() -> None:
    pairs = _lines(PAIRS)
    qa = _lines(QA)
    if not pairs and not qa:
        print(f"没有评估样本：{PAIRS.name} 和 {QA.name} 都是空的或不存在")
        return
    recall = _recall(pairs) if pairs else {}
    ragas = _ragas(qa) if qa else {}
    with db.connect() as conn:
        conn.execute(
            "INSERT INTO kb_eval_run (id, ragas, recall) VALUES (%s, %s, %s)",
            (str(uuid.uuid4()), Jsonb(ragas), Jsonb(recall)),
        )
    print(json.dumps({"ragas": ragas, "recall": recall}, ensure_ascii=False, indent=2))


def _lines(path: Path) -> list[dict]:
    if not path.is_file():
        return []
    rows = []
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if line:
            rows.append(json.loads(line))
    return rows


def _tenant(chunk_id: str) -> str:
    with db.connect() as conn:
        row = conn.execute(
            "SELECT tenant_id FROM kb_chunk WHERE id = %s",
            (chunk_id,),
        ).fetchone()
    if row is None:
        raise SystemExit(f"找不到块 {chunk_id}。通过审核之后块 id 会变，请按新 id 重写对照。")
    return row[0]


def score_ranks(ranks: list[int | None]) -> dict:
    """R@1、R@5、R@10 和 MRR。没命中的名次不计入 Recall，MRR 记 0。"""
    total = len(ranks)
    if total == 0:
        return {"n": 0, "recall_at_1": 0.0, "recall_at_5": 0.0, "recall_at_10": 0.0, "mrr": 0.0}

    def at(k: int) -> float:
        return sum(1 for rank in ranks if rank is not None and rank <= k) / total

    mrr = sum((1.0 / rank) if rank else 0.0 for rank in ranks) / total
    return {
        "n": total,
        "recall_at_1": at(1),
        "recall_at_5": at(5),
        "recall_at_10": at(10),
        "mrr": mrr,
    }


def _recall(pairs: list[dict]) -> dict:
    ranks = []
    for row in pairs:
        expected = row["expected_id"]
        hits = search_question(_tenant(expected), row["question"], limit=10)
        rank = next((i for i, hit in enumerate(hits, 1) if hit["chunk_id"] == expected), None)
        ranks.append(rank)
    return score_ranks(ranks)


def _ragas(rows: list[dict]) -> dict:
    from eval.metrics import adapt_chinese, build_metrics, make_dataset
    from eval.run import get_evaluator

    filled = []
    for row in rows:
        tenant = row.get("tenant_id") or ""
        if not tenant and row.get("expected_id"):
            tenant = _tenant(row["expected_id"])
        if not tenant:
            raise SystemExit("kb_qa.jsonl 每行要有 tenant_id，或带 expected_id 以便从块上读租户")
        out = run_ask(tenant_id=tenant, question=row["question"])
        if out["abstained"] or out["answer"] == ABSTAIN:
            continue
        filled.append({
            "user_input": row["question"],
            "response": out["answer"],
            "retrieved_contexts": [format_hit(hit) for hit in out["hits"]],
            "reference": row.get("reference") or "",
        })
    if not filled:
        return {"skipped": "没有有据回答"}
    llm, emb = get_evaluator()
    if llm is None:
        return {"skipped": "未配置 DASHSCOPE_API_KEY"}
    from ragas import evaluate

    metrics = adapt_chinese(build_metrics(llm, emb)[:3])
    frame = evaluate(dataset=make_dataset(filled), metrics=metrics).to_pandas()
    report = {"n": len(filled)}
    for col in ("faithfulness", "answer_relevancy", "llm_context_precision_without_reference"):
        if col in frame.columns:
            report[col] = float(frame[col].mean())
    return report


if __name__ == "__main__":
    main()
