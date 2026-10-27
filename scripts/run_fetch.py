"""
scripts/run_fetch.py —— 抓取 worker（手动/定时触发，不在请求链路里）

用法：
  python -m scripts.run_fetch                    # 处理最多 5 个排队任务
  python -m scripts.run_fetch --limit 1          # 只处理 1 个（演示最稳）
  python -m scripts.run_fetch --task-id 3        # 只处理指定任务，便于调试
  python -m scripts.run_fetch --recover 15       # 先把卡在 running 超 15 分钟的任务放回队列
  python -m scripts.run_fetch --status           # 只看现状，不动数据

为什么不做成 FastAPI BackgroundTasks：
  抓取要 2 次外部 API + 可能一次模型调用，10~40 秒；进程重启会丢任务，
  还得额外做恢复。做成独立命令后：可手动重跑、可上定时任务、可单步调试，
  也符合「抓取属于建库动作，不该挂在用户请求上」的分工。
"""
from __future__ import annotations

import argparse
import logging

import psycopg

import config
from kg import fetch


def _status() -> None:
    with psycopg.connect(config.PG_DSN) as conn:
        rows = conn.execute(
            "SELECT status, count(*) FROM kg_fetch_task GROUP BY status ORDER BY status"
        ).fetchall()
        bundles = conn.execute(
            "SELECT status, count(*) FROM kg_food_bundle GROUP BY status ORDER BY status"
        ).fetchall()
    print("抓取任务：", dict(rows) or "（空）")
    print("产物 bundle：", dict(bundles) or "（空）")
    print(f"今日预算：{fetch.budget_used_today()}/{config.FOOD_FETCH_DAILY_BUDGET}")
    print(f"USDA key：{config.FDC_API_KEY[:8]}…  画像模式：{config.FOOD_DRAFT_MODE}")


def main() -> int:
    p = argparse.ArgumentParser()
    p.add_argument("--limit", type=int, default=5, help="本次最多处理几个任务")
    p.add_argument("--task-id", type=int, default=None, help="只处理指定任务")
    p.add_argument("--recover", type=int, default=None, metavar="MINUTES",
                   help="把 running 超过 N 分钟的任务放回队列")
    p.add_argument("--status", action="store_true", help="只查看现状")
    args = p.parse_args()

    logging.basicConfig(
        level=logging.DEBUG if config.DEBUG else logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
    )

    if args.status:
        _status()
        return 0

    if args.recover is not None:
        n = fetch.recover_stale(args.recover)
        print(f"已恢复 {n} 个任务")

    results = fetch.run(limit=args.limit, task_id=args.task_id)
    if not results:
        print("没有待处理的任务。")
        return 0

    for r in results:
        if r.get("status") == "budget_exhausted":
            print(f"今日预算已用尽（{r['used']}），本次不处理。")
            continue
        line = f"#{r.get('task_id')} {r.get('food_key')} → {r.get('status')}"
        if r.get("matched"):
            line += f"\n    命中：{r['matched']}"
            line += f"\n    评分：{r.get('score')}（{r.get('bundle_status')}）"
            line += f"\n    营养素：{r.get('nutrients')} 项"
        if r.get("veto"):
            line += f"\n    [否决] {r['veto']}"
        if r.get("reason"):
            line += f"\n    原因：{r['reason']}"
        print(line)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
