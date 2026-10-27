"""
eval/run.py —— 跑评估，产出基线报告

用法：
  python -m eval.run --dataset eval/dataset.jsonl --run-chain   # 推荐
  python -m eval.run --dataset eval/dataset.jsonl               # 只评已有 response

流程：读评估集 →（可选）用真实链路填 answer + contexts → 构造样本 → 跑 Ragas → 输出报告。

⚠️ 不传 --run-chain 时，dataset.jsonl 里的 response 是空串，
   那样评出来的 Faithfulness / Relevancy 评的是「空回答」，分数没有意义。
"""
from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Any

import config


def load_dataset(path: str) -> list[dict[str, Any]]:
    rows = []
    with open(path, encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if line:
                rows.append(json.loads(line))
    return rows


def get_evaluator():
    """返回 DashScope 的 evaluator LLM + embedding。

    评估器要的是原生 BaseChatModel（Ragas 的 LangchainLLMWrapper 只吃这个），
    所以取池内当前队首的单模型，而不是 Agent 中间件或回退链。

    评估模型用更大的 max_tokens：ragas 的 Faithfulness 要生成 statement 列表、
    ResponseRelevancy 要生成多个问题，2048 的默认值会触发 LLMDidNotFinish。
    """
    from rag.embed import get_embeddings
    from llm import primary_model

    if not config.DASHSCOPE_API_KEY:
        return None, get_embeddings()
    return primary_model(max_tokens=config.LLM_MAX_TOKENS_CEILING), get_embeddings()


def fill_by_chain(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """用真实链路把 response 与 retrieved_contexts 填上。

    不做这一步，评估的就是空回答 —— 分数再好看也没有意义。
    contexts 取的是 assemble 节点组装的上下文原文，
    也就是 generate 真正看到的东西，这样 Faithfulness 才有对照物。
    """
    from graph.workflow import get_agent

    agent = get_agent()
    for r in rows:
        state = {"question": r["user_input"], "user_id": "eval-user", "session_id": None}
        out = agent.invoke(state)
        r["response"] = out.get("answer", "") or ""
        ctx = out.get("_memory_context", "") or ""
        r["retrieved_contexts"] = [line for line in ctx.split("\n") if line.strip()]
    print(f"链路执行完成：{len(rows)} 条样本已填充 answer 与 contexts")
    return rows


def _categorize(row: dict[str, Any]) -> str:
    """把样本按 reference 分成三类，分别用不同指标评估。

    faithfulness / relevancy / context_precision 只对「有据回答」有意义 ——
    弃答题（ABSTAIN）和未收录题（UNCATALOGUED）的正确答案是拒答/登记，
    没有可验证的 claim，混在一起会把 faithfulness 拖到一个误导性的低分。
    所以：有据题跑 LLM 三件套，弃答题跑 abstention，未收录题跑 uncatalogued。
    """
    ref = (row.get("reference") or "").strip().upper()
    if ref == "ABSTAIN":
        return "abstain"
    if ref == "UNCATALOGUED":
        return "uncatalogued"
    return "grounded"


def main():
    p = argparse.ArgumentParser()
    p.add_argument("--dataset", default="eval/dataset.jsonl")
    p.add_argument("--out", default="eval/baseline_report.json")
    p.add_argument(
        "--run-chain",
        action="store_true",
        help="先用真实链路跑一遍再评估（需要 Neo4j / Milvus / LLM 全部就绪）",
    )
    args = p.parse_args()

    rows = load_dataset(args.dataset)
    if not rows:
        print(f"评估集为空：{args.dataset}")
        return

    if args.run_chain:
        rows = fill_by_chain(rows)
    else:
        empty = sum(1 for r in rows if not (r.get("response") or "").strip())
        if empty:
            print(
                f"⚠️  {empty}/{len(rows)} 条样本 response 为空：当前等于在评「空回答」，"
                f"分数没有参考价值。加 --run-chain 先用真实链路跑一遍。\n"
            )

    from eval.metrics import build_metrics, adapt_chinese, make_dataset

    llm, emb = get_evaluator()
    if llm is None:
        print("未配置 DASHSCOPE_API_KEY，无法初始化 evaluator LLM。")
        return

    from ragas import evaluate

    # ---------- 分层评估 ----------
    grounded = [r for r in rows if _categorize(r) == "grounded"]
    abstain = [r for r in rows if _categorize(r) == "abstain"]
    uncat = [r for r in rows if _categorize(r) == "uncatalogued"]

    report: dict[str, Any] = {
        "dataset": args.dataset,
        "num_samples": len(rows),
        "breakdown": {
            "grounded": len(grounded),
            "abstain": len(abstain),
            "uncatalogued": len(uncat),
        },
        "metrics": {},
    }

    def _mean(df, cols):
        for col in cols:
            if col in df.columns:
                report["metrics"][col] = float(df[col].mean())

    # ① 有据回答 → faithfulness / relevancy / context_precision
    if grounded:
        m = adapt_chinese(build_metrics(llm, emb)[:3])  # 前 3 个是 LLM 指标
        df = evaluate(dataset=make_dataset(grounded), metrics=m).to_pandas()
        _mean(df, ["faithfulness", "answer_relevancy",
                   "llm_context_precision_without_reference"])

    # ② 弃答题 → abstention_correctness
    if abstain:
        from eval.metrics import AbstentionCorrectness
        m = adapt_chinese([AbstentionCorrectness()])
        df = evaluate(dataset=make_dataset(abstain), metrics=m).to_pandas()
        _mean(df, ["abstention_correctness"])

    # ③ 未收录题 → uncatalogued_handling
    if uncat:
        from eval.metrics import UncataloguedHandling
        m = adapt_chinese([UncataloguedHandling()])
        df = evaluate(dataset=make_dataset(uncat), metrics=m).to_pandas()
        _mean(df, ["uncatalogued_handling"])

    out_path = Path(args.out)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")

    print("=== 基线报告（分层）===")
    print(json.dumps(report, ensure_ascii=False, indent=2))
    print(f"\n报告已写入 {out_path}")


if __name__ == "__main__":
    main()
