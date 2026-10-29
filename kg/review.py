"""
kg/review.py —— 候选关系审核 CLI

用法：
  python -m kg.review list --status pending --limit 20      # 列出待审
  python -m kg.review approve --ids 1,2,3                  # 批准
  python -m kg.review reject --ids 4                       # 驳回
  python -m kg.review stats                                # 统计各状态数量
"""
from __future__ import annotations

import argparse
import json

import psycopg

import config

import db



def cmd_list(args):
    with db.connect() as conn:
        cur = conn.cursor()
        cur.execute(
            "SELECT id, src_id, rel, dst_id, dst_name, claim_text, evidence_type, "
            "evidence_level, confidence FROM kg_candidate WHERE status = %s "
            "ORDER BY id LIMIT %s",
            (args.status, args.limit),
        )
        rows = cur.fetchall()
    if not rows:
        print(f"没有 {args.status} 状态的候选。")
        return
    for r in rows:
        print(
            f"[{r[0]}] {r[1]} -{r[2]}-> {r[4] or r[3]}  "
            f"({r[6]}/{r[7]}, conf={r[8]:.2f})\n    {r[5]}\n"
        )


def cmd_approve(args):
    ids = [int(x) for x in args.ids.split(",") if x.strip()]
    with db.connect() as conn:
        conn.execute(
            "UPDATE kg_candidate SET status = 'approved', reviewer = %s, reviewed_at = now() "
            "WHERE id = ANY(%s)",
            (args.reviewer, ids),
        )
    print(f"已批准 {len(ids)} 条候选。下一步运行 merge 进主图。")


def cmd_reject(args):
    ids = [int(x) for x in args.ids.split(",") if x.strip()]
    with db.connect() as conn:
        conn.execute(
            "UPDATE kg_candidate SET status = 'rejected', reviewer = %s, reviewed_at = now() "
            "WHERE id = ANY(%s)",
            (args.reviewer, ids),
        )
    print(f"已驳回 {len(ids)} 条候选。")


def cmd_stats(args):
    with db.connect() as conn:
        cur = conn.cursor()
        cur.execute("SELECT status, count(*) FROM kg_candidate GROUP BY status ORDER BY status")
        rows = cur.fetchall()
    total = sum(r[1] for r in rows)
    print(f"候选总数：{total}")
    for status, n in rows:
        print(f"  {status}: {n}")


def main():
    p = argparse.ArgumentParser()
    p.add_argument("--reviewer", default="admin")
    sub = p.add_subparsers(dest="cmd", required=True)

    sp_list = sub.add_parser("list")
    sp_list.add_argument("--status", default="pending")
    sp_list.add_argument("--limit", type=int, default=20)
    sp_list.set_defaults(fn=cmd_list)

    sp_app = sub.add_parser("approve")
    sp_app.add_argument("--ids", required=True)
    sp_app.set_defaults(fn=cmd_approve)

    sp_rej = sub.add_parser("reject")
    sp_rej.add_argument("--ids", required=True)
    sp_rej.set_defaults(fn=cmd_reject)

    sp_st = sub.add_parser("stats")
    sp_st.set_defaults(fn=cmd_stats)

    args = p.parse_args()
    args.fn(args)


if __name__ == "__main__":
    main()
