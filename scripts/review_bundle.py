"""
scripts/review_bundle.py —— 未收录食材补录的审核 CLI（Phase 9.3）

用法：
  python -m scripts.review_bundle list                      # 待审清单
  python -m scripts.review_bundle show 3                    # 看某一份的全部字段与营养素
  python -m scripts.review_bundle approve 3                 # 通过并合并进主图
  python -m scripts.review_bundle approve 3 --items 12,13   # 只采用部分项（逐条审）
  python -m scripts.review_bundle approve 3 --no-merge      # 只生成候选，稍后手动 merge
  python -m scripts.review_bundle reject 3 --reason "物种不对，是黑菜豆"
  python -m scripts.review_bundle merge                     # 补做合并（上次 Neo4j 挂了之后）

为什么是 CLI 而不是先做网页：审核是低频、高责任的动作（一天几条），
CLI 能一行命令看到全部原始字段、能重放、能进脚本；网页要额外处理鉴权、
并发、前端状态，收益在这个阶段很低。接口层已经在 api/admin.py 就绪了，
要做页面随时能接。
"""
from __future__ import annotations

import argparse
import logging
import sys

import config
from kg import promote

logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s %(message)s")


def _mask_sources(sources: list) -> str:
    for s in sources or []:
        if isinstance(s, dict) and s.get("url"):
            return f"{s.get('title', '')} · {s['url']}"
    return "（无来源记录）"


def cmd_list(args) -> int:
    rows = promote.list_bundles(args.status, args.limit)
    if not rows:
        print(f"没有 {args.status} 状态的补录产物。")
        return 0
    print(f"{args.status} 共 {len(rows)} 份：\n")
    for r in rows:
        flag = " ⚠️硬否决" if r["hard_veto"] else ""
        cid = f" → {r['canonical_id']}" if r["canonical_id"] else ""
        print(
            f"[{r['id']}] {r['food_key']}  score={r['score_total']:.2f} "
            f"来源数={r['source_count']}{flag}{cid}"
        )
        print(f"     {r['canonical_name']} / {r['english_name']} / {r['scientific_name']}")
        print(f"     原始描述：{r['source_desc']}")
        if r["hard_veto"]:
            print(f"     否决理由：{r['hard_veto']}")
        print()
    return 0


def cmd_show(args) -> int:
    b = promote.get_bundle(args.bundle_id)
    print(f"[{b['id']}] {b['food_key']}   状态={b['status']}   评分={b['score_total']:.2f}")
    print(f"  中文名    ：{b['canonical_name']}")
    print(f"  英文名    ：{b['english_name']}")
    print(f"  学名      ：{b['scientific_name'] or '（空）'}")
    print(f"  分类      ：{b['category']}")
    print(f"  原始描述  ：{b['source_desc']}")
    print(f"  外部主键  ：{b['source_key']}:{b['external_id']}")
    print(f"  来源      ：{_mask_sources(b['sources'])}")
    print(f"  来源数    ：{b['source_count']}（< {config.FOOD_TRUSTED_MIN_SOURCES} 会标单源）")
    print(f"  评分明细  ：{b['score_detail']}")
    if b["hard_veto"]:
        print(f"  ⚠️ 硬否决 ：{b['hard_veto']}")
    if b["canonical_id"]:
        print(f"  已决议    ：{b['canonical_id']}")
    if b["reject_reason"]:
        print(f"  驳回原因  ：{b['reject_reason']}")

    print("\n  逐项（item id 可用于 --items）：")
    for it in b["items"]:
        flag = "✗" if it["item_status"] == "rejected" else " "
        unit = f" {it['unit']}" if it["unit"] else ""
        print(
            f"    {flag} #{it['id']:<4} {it['item_type']:<8} {it['field_name']:<16} "
            f"{it['field_value']}{unit:<6} ← {it['source_locator']}"
        )
    if b["node_candidates"]:
        print("\n  已生成的节点候选：")
        for n in b["node_candidates"]:
            print(f"    {n['node_type']:<10} {n['canonical_id']:<12} {n['name']}")
    return 0


def cmd_approve(args) -> int:
    item_ids = [int(x) for x in args.items.split(",")] if args.items else None
    try:
        r = promote.approve_bundle(
            args.bundle_id,
            reviewer=args.reviewer,
            item_ids=item_ids,
            merge_now=not args.no_merge,
        )
    except promote.PromoteError as exc:
        print(f"❌ 不能通过：{exc}", file=sys.stderr)
        return 1

    identity = {
        "created": "新建节点",
        "existing_alias": "挂到既有节点（别名已存在）",
        "existing_candidate": "复用上次分配但未入图的节点",
    }.get(r["identity"], r["identity"])
    print(f"✅ bundle {r['bundle_id']} 已通过")
    print(f"   身份判定：{identity} → {r['canonical_id']}")
    print(f"   节点 {r['nodes']} 个 / 边 {r['edges']} 条")
    if r["provisional"]:
        print(
            f"   标记 provisional：只有单一来源（< {config.FOOD_TRUSTED_MIN_SOURCES}），"
            "检索时会降级引用"
        )
    if "merge" in r:
        m = r["merge"]
        print(f"   合并：节点 {m['nodes']} / 边 {m['edges']}")
        for a in m["aliases"]:
            print(f"   别名写入 {a['canonical_id']}：{', '.join(a['written']) or '（无）'}")
            for s in a["skipped"]:
                print(f"     ⚠️ 跳过「{s['alias']}」——已被 {s['occupied_by']} 占用")
    else:
        print("   未合并。稍后执行 python -m scripts.review_bundle merge")
    return 0


def cmd_reject(args) -> int:
    try:
        promote.reject_bundle(args.bundle_id, reviewer=args.reviewer, reason=args.reason)
    except promote.PromoteError as exc:
        print(f"❌ {exc}", file=sys.stderr)
        return 1
    print(f"已驳回 bundle {args.bundle_id}：{args.reason}")
    print("   抓取任务与登记请求已一并关闭，不会每天重抓同一个词。")
    return 0


def cmd_merge(args) -> int:
    m = promote.merge_pending(args.bundle)
    print(f"合并完成：节点 {m['nodes']} / 边 {m['edges']}")
    for a in m["aliases"]:
        print(f"   别名写入 {a['canonical_id']}：{', '.join(a['written']) or '（无）'}")
    return 0


def main() -> int:
    p = argparse.ArgumentParser(description="未收录食材补录审核")
    p.add_argument("--reviewer", default="admin")
    sub = p.add_subparsers(dest="cmd", required=True)

    sp = sub.add_parser("list", help="待审清单")
    sp.add_argument("--status", default="pending_review")
    sp.add_argument("--limit", type=int, default=20)
    sp.set_defaults(fn=cmd_list)

    sp = sub.add_parser("show", help="看一份产物的全部内容")
    sp.add_argument("bundle_id", type=int)
    sp.set_defaults(fn=cmd_show)

    sp = sub.add_parser("approve", help="通过并合并进主图")
    sp.add_argument("bundle_id", type=int)
    sp.add_argument("--items", default="", help="只采用这些 item id（逗号分隔）")
    sp.add_argument("--no-merge", action="store_true", help="只生成候选，不写主图")
    sp.set_defaults(fn=cmd_approve)

    sp = sub.add_parser("reject", help="驳回")
    sp.add_argument("bundle_id", type=int)
    sp.add_argument("--reason", required=True)
    sp.set_defaults(fn=cmd_reject)

    sp = sub.add_parser("merge", help="补做合并")
    sp.add_argument("--bundle", type=int, default=None)
    sp.set_defaults(fn=cmd_merge)

    args = p.parse_args()
    return args.fn(args)


if __name__ == "__main__":
    sys.exit(main())
