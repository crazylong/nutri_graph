"""
scripts/import_fcd.py —— 中国食物成分表（离线抓取包）并入审核链路

数据来源：fcd_crawler 抓下来的 foods.json（nlc.chinanutri.cn/fq，中国疾控中心营养与健康所）。
抓取在仓库外做（见该目录 README），本脚本只负责「把抓到的文件变成待审 bundle」。

为什么不直接写主图：
  抓来的数据和网友上传的表是同一类东西 —— 都是「某食材每 100g 含什么」。
  它必须和 Excel 导入走同一条链路，否则会出现「页面上有、问答里没有」的两套数据。
  于是：

      foods.json → 映射成模板行 → importer 校验 → kg_food_bundle(pending_review)
                 → 审核 → kg.merge → Neo4j → 问答 / 图谱探索都查得到

复用 importer 校验的三个理由（不是顺手，是必须）：
  1. 分类枚举、名称长度、营养素上限、「至少 4 项」这些规则只有一份；
  2. 单位换算只在这里做一次（原表能量是 kJ，模板契约是 kcal）；
  3. 校验不通过的行走不到 stage_bundles，脏数据进不了队列。

用法：
  python -m scripts.import_fcd --check                 # 只映射+校验，打印分布，不写库
  python -m scripts.import_fcd --limit 20              # 只导前 20 条，进待审队列
  python -m scripts.import_fcd                         # 全量进待审队列
  python -m scripts.import_fcd --approve --limit 20    # 入队后立刻审核通过并合并

  --file 指定 foods.json 路径（默认 data/external/fcd_foods.json，
        再退回仓库外的 ../fcd_crawler/data/foods.json）
  --dry-run 等价于 --check
"""
from __future__ import annotations

import argparse
import json
import logging
import sys
import unicodedata
import uuid
from pathlib import Path

import psycopg
from psycopg.types.json import Jsonb

import config
from kg import import_bundle, importer, promote

log = logging.getLogger("scripts.import_fcd")

SOURCE_KEY = import_bundle.FCD_SOURCE_KEY

# 抓取包的分类（21 个）→ 图谱分类枚举（14 个）。
# 对不上的不要硬猜，一律「其他」；宁可分类粗，也不要让「速食食品」混进「谷物」。
CATEGORY_MAP: dict[str, str] = {
    "谷类及制品": "谷物",
    "薯类、淀粉及制品": "谷物",
    "干豆类及制品": "豆类",
    "蔬菜类及制品": "蔬菜",
    "菌藻类": "菌藻",
    "水果类及制品": "水果",
    "坚果、种子类": "坚果",
    "畜肉类及制品": "肉类",
    "禽肉类及制品": "禽类",
    "蛋类及制品": "蛋类",
    "乳类及制品": "奶类",
    "鱼虾蟹贝类": "水产",
    "油脂类": "油脂",
    "调味品类": "调味",
    # 下面这些在 14 个枚举里没有对应项，归「其他」
    "速食食品": "其他",
    "小吃、甜饼": "其他",
    "饮料类": "其他",
    "含酒精饮料": "其他",
    "糖、果脯和蜜饯、蜂蜜": "其他",
    "婴幼儿食品": "其他",
    "其它": "其他",
}

# 抓取包的营养素 key → 模板列 key。
# 只有这里列出来的才会进图谱 —— 其余（水分/灰分/食部）不是营养素，
# 显式不导；剩下没导的项图谱里没有对应节点，硬塞会被 nutrients.resolve()
# 静默丢掉，不如显式不导。
#
# 2026-10-28 补齐第二批：原表 31 项里原先只导 13 项 + 能量，磷/硒/铜/锰/碘/
# 胆固醇/B 族/维 E/三种脂肪酸都被丢掉。它们在图谱里已经有固定 NUT id
# （见 data/crosswalk/nutrient_map.csv），这里补上映射即可。
# 三种脂肪酸的单位是「占总能量百分比」（%E），**和脂肪的 g 不是一个量纲**，
# 所以节点单位就写 %E，不做任何换算。
NUTRIENT_MAP: dict[str, str] = {
    "protein": "protein_g",
    "fat": "fat_g",
    "carbohydrate": "carb_g",
    "dietary_fiber": "fiber_g",
    "sodium": "sodium_mg",
    "calcium": "calcium_mg",
    "iron": "iron_mg",
    "zinc": "zinc_mg",
    "potassium": "potassium_mg",
    "magnesium": "magnesium_mg",
    "vitamin_c": "vitamin_c_mg",
    "vitamin_a": "vitamin_a_ug",
    "carotene": "carotene_ug",
    # 矿物质（第二批）
    "phosphorus": "phosphorus_mg",
    "selenium": "selenium_ug",
    "copper": "copper_mg",
    "manganese": "manganese_mg",
    "iodine": "iodine_ug",
    # 维生素（第二批）：原表把 B1 写作「硫胺素」、B2 写作「核黄素」、维 E 按 α-TE 计
    "thiamin": "vitamin_b1_mg",
    "riboflavin": "vitamin_b2_mg",
    "niacin": "niacin_mg",
    "alpha_te": "vitamin_e_mg",
    # 其他（第二批）
    "cholesterol": "cholesterol_mg",
    # 脂肪酸构成（%E = 占总能量百分比）
    "sfa": "sfa_pct",
    "mufa": "mufa_pct",
    "pufa": "pufa_pct",
}

# 原表能量单位是 kJ，模板契约是 kcal/100g
KJ_PER_KCAL = 4.184

SOURCE_LABEL = "中国食物成分表 · nlc.chinanutri.cn/fq"


def _clean(v) -> str:
    return unicodedata.normalize("NFKC", str(v or "")).strip()


def _alias(raw: str) -> str:
    """原表别名形如「商品名：美味花生。」—— 去掉标签和句号才是能用的别名。"""
    s = _clean(raw).rstrip("。.")
    for prefix in ("商品名：", "商品名:", "别名：", "别名:"):
        if s.startswith(prefix):
            s = s[len(prefix):].strip()
            break
    return s


def _num(nut: dict, key: str) -> float | None:
    cell = (nut or {}).get(key) or {}
    v = cell.get("value")
    if v is None:
        return None
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


def to_row(item: dict) -> dict[str, object]:
    """一条抓取记录 → 模板列的值（键是 importer.Column.key）。

    只做两件「确定」的事：分类查表、能量 kJ→kcal。
    缺的营养素留空 —— 原表本身为空，填 0 是编数据。
    """
    nut = item.get("nutrients") or {}
    cat_raw = _clean(item.get("cat"))
    vals: dict[str, object] = {
        "name": _clean(item.get("name")),
        "aliases": _alias(item.get("alias", "")),
        "category": CATEGORY_MAP.get(cat_raw, "其他"),
        "nature": "",  # 成分表没有性味，留空，等人工补
        "english_name": _clean(item.get("english_name")),
        "scientific_name": "",
        "source": f"{SOURCE_LABEL} · id={item.get('food_id')}",
        "note": f"原分类：{cat_raw}；抓取于 {_clean(item.get('fetched_at'))[:10]}",
    }
    for src_key, col_key in NUTRIENT_MAP.items():
        v = _num(nut, src_key)
        if v is not None:
            vals[col_key] = v
    kj = _num(nut, "energy")
    if kj is not None:
        # 保留 1 位小数：kcal 的精度本就不该高于 1（原表 kJ 也是整数位）
        vals["energy_kcal"] = round(kj / KJ_PER_KCAL, 1)
    return vals


def build_results(items: list[dict]) -> list[importer.RowResult]:
    mapped = list(importer.COLUMNS)
    results: list[importer.RowResult] = []
    for idx, item in enumerate(items, start=2):  # 行号从 2 开始：第 1 行是表头
        vals = to_row(item)
        row = [vals.get(c.key, "") for c in importer.COLUMNS]
        # relax_required：成分表里空白 = 未测定，不是漏填。
        # 不放松的话，全部含酒精饮料（47 条无蛋白/脂肪/碳水）和食用油（19 条无碳水）
        # 会被当成「必填为空」整批拒掉 —— 那是数据本身如此，不是错误。
        results.append(
            importer.validate_row(idx, row, mapped, relax_required=True)
        )
    return results


def _existing_in_graph(norms: list[str]) -> set[str]:
    """主图里已有同名食材 → 审核后会挂到既有节点，不算新增。"""
    if not norms:
        return set()
    try:
        with psycopg.connect(config.PG_DSN) as conn:
            cur = conn.cursor()
            cur.execute(
                "SELECT alias_norm FROM dict_alias "
                "WHERE entity_type = 'Ingredient' AND alias_norm = ANY(%s)",
                (norms,),
            )
            return {r[0] for r in cur.fetchall()}
    except psycopg.errors.UndefinedTable:
        return set()


def _already_merged(norms: list[str]) -> set[str]:
    """本来源下已经并进主图的 food_key。

    脚本必须可重跑：如果只看「未结 bundle」，已经 merged 的食材会被当成新行
    再导一遍，于是同一个食材多出一份候选、营养边翻倍。这里的判断只挡 merged，
    pending_review / approved 的照旧覆盖 —— 那是「重导一次改数值」的正常用法。
    """
    if not norms:
        return set()
    with psycopg.connect(config.PG_DSN) as conn:
        cur = conn.execute(
            "SELECT food_key FROM kg_food_bundle "
            "WHERE source_key = %s AND status = 'merged' AND food_key = ANY(%s)",
            (SOURCE_KEY, norms),
        )
        return {r[0] for r in cur.fetchall()}


def reconcile_merged() -> int:
    """把「候选节点已进图、但 bundle 还挂在 approved」的批次补成 merged。

    为什么会剩下这种行：同一 food_key 在抓取包里出现两次（煎饼、麦片、色拉油…），
    第二行的 bundle 和第一行指向同一个 canonical_id；第一行合并时把候选节点
    置成 merged，第二行再 approve 时改的仍是那一条候选，merge 只捞 approved，
    于是它的 bundle 状态留在 approved —— 数据其实已经在图里了，只是状态没跟上。
    不补的话，看板上会永久显示「已通过」但页面上看不到，像丢数据。
    """
    with psycopg.connect(config.PG_DSN) as conn:
        cur = conn.execute(
            "UPDATE kg_food_bundle b SET status = 'merged' "
            "WHERE b.source_key = %s AND b.status = 'approved' "
            "  AND b.canonical_id IS NOT NULL "
            "  AND EXISTS (SELECT 1 FROM kg_node_candidate c "
            "              WHERE c.canonical_id = b.canonical_id AND c.status = 'merged')",
            (SOURCE_KEY,),
        )
        return cur.rowcount


def print_summary(results: list[importer.RowResult]) -> None:
    s = importer.summarize(results)
    print(
        f"共 {s['total']} 行：可导入 {s['importable']}"
        f"（ok {s['ok']} / 有提醒 {s['warn']}）"
        f"，会跳过 {s['error'] + s['duplicate_file']}（错 {s['error']} / 文件内重名 {s['duplicate_file']}）"
    )

    bad = [r for r in results if r.verdict == "error"]
    if bad:
        # 只打印原因分布，不逐条刷屏；要逐条看用 --show-error
        from collections import Counter

        reasons = Counter(
            m.text.split("（")[0] for r in bad for m in r.messages if m.level == "error"
        )
        print("\n跳过原因分布：")
        for text, n in reasons.most_common(10):
            print(f"  {n:>4}  {text}")

    dup = [r for r in results if r.verdict == "duplicate_file"]
    if dup:
        print(f"\n文件内重名（只留首行）：{'、'.join(r.name for r in dup[:10])}")


def _chunks(seq: list, size: int):
    for i in range(0, len(seq), size):
        yield seq[i:i + size]


def stage(results: list[importer.RowResult], filename: str, approve: bool) -> dict[str, int]:
    """写批次 + 逐行结论 + bundle（pending_review），可选随后审核通过。"""
    todo = [r for r in results if r.verdict in ("ok", "warn", "duplicate_db")]
    if not todo:
        print("\n没有可入队的行。")
        return {"jobs": 0, "staged": 0, "approved": 0}

    totals = {"jobs": 0, "staged": 0, "created": 0, "updated": 0, "approved": 0}
    with psycopg.connect(config.PG_DSN) as conn:
        staged_rows = []
        for part in _chunks(todo, importer.MAX_ROWS):
            job_id = uuid.uuid4()
            s = importer.summarize(part)
            conn.execute(
                "INSERT INTO kg_import_job (job_id, filename, client_ip, template_version, "
                "  source_key, total_rows, ok_rows, warn_rows, error_rows, status) "
                "VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,'previewed')",
                (
                    job_id, filename[:200], "crawler", importer.TEMPLATE_VERSION,
                    SOURCE_KEY, s["total"],
                    s["ok"] + s["duplicate_db"], s["warn"],
                    s["error"] + s["duplicate_file"],
                ),
            )
            with conn.cursor() as cur:
                cur.executemany(
                    "INSERT INTO kg_import_row (job_id, row_no, name, verdict, messages, payload) "
                    "VALUES (%s,%s,%s,%s,%s,%s)",
                    [
                        (
                            job_id, r.row_no, r.name[:80], r.verdict,
                            Jsonb([m.as_dict() for m in r.messages]), Jsonb(r.payload),
                        )
                        for r in part
                    ],
                )
            staged_rows.append((job_id, part))
            totals["jobs"] += 1

    # 落 bundle 要在批次行上库之后 —— stage_bundles 自己开事务，
    # 批次和 bundle 分属两个事务（和 HTTP 那条路径一致），
    # 所以中途失败只会留下「空批次」，不会留下半份 bundle。
    for job_id, part in staged_rows:
        out = import_bundle.stage_bundles(part, job_id, filename, source_key=SOURCE_KEY)
        totals["staged"] += len(out)
        totals["created"] += sum(1 for x in out if x["action"] == "created")
        totals["updated"] += sum(1 for x in out if x["action"] == "updated")
        # 批次状态和 HTTP 那条路径保持一致：入队成功就是 committed，
        # 留在 previewed 会让 /kg/import/jobs 看起来像「有批没提交」。
        with psycopg.connect(config.PG_DSN) as conn:
            conn.execute(
                "UPDATE kg_import_job SET status = 'committed', committed_at = now() "
                "WHERE job_id = %s",
                (job_id,),
            )
        print(f"  批次 {job_id} 入队 {len(out)} 条")

        if approve:
            for x in out:
                try:
                    # merge_now=False：逐条合并会把 Neo4j 打 1356 次，
                    # 这里只生成候选，整批结束后一次 merge_pending。
                    promote.approve_bundle(x["bundle_id"], reviewer="crawler:bulk", merge_now=False)
                    totals["approved"] += 1
                except Exception as exc:  # 单条失败不该拖垮整批
                    log.warning("审核通过失败 bundle=%s %s", x["bundle_id"], exc)

    if approve and totals["approved"]:
        print("\n合并进图谱…")
        try:
            n = promote.merge_pending()
            print(f"  merge 完成：{n.get('nodes', 0)} 个节点 / {n.get('edges', 0)} 条边")
        except Exception as exc:
            log.error("merge 失败：%s", exc)
            print(f"  merge 失败：{exc}（稍后跑 python -m scripts.review_bundle merge 补做）")
        fixed = reconcile_merged()
        if fixed:
            print(f"  补齐 {fixed} 份「已进图但状态还是 approved」的批次")

    return totals


def _default_file() -> Path:
    here = Path(__file__).resolve().parent.parent
    for cand in (
        here / "data" / "external" / "fcd_foods.json",
        here.parent / "fcd_crawler" / "data" / "foods.json",
    ):
        if cand.exists():
            return cand
    return here / "data" / "external" / "fcd_foods.json"


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="把中国食物成分表抓取包并入审核链路")
    ap.add_argument("--file", type=Path, default=None, help="foods.json 路径")
    ap.add_argument("--limit", type=int, default=0, help="只导前 N 条（0 = 全部）")
    ap.add_argument("--check", "--dry-run", dest="check", action="store_true",
                    help="只映射+校验，不写库")
    ap.add_argument("--approve", action="store_true",
                    help="入队后立刻审核通过并合并进图谱")
    ap.add_argument("--show-error", action="store_true", help="逐条打印被跳过的行")
    ap.add_argument("--reconcile", action="store_true",
                    help="只补齐状态（已进图但 bundle 还是 approved），不导入")
    args = ap.parse_args(argv)

    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s %(message)s")

    if args.reconcile:
        n = reconcile_merged()
        print(f"补齐 {n} 份批次状态为 merged")
        return 0

    path = args.file or _default_file()
    if not path.exists():
        print(f"找不到抓取包：{path}\n先跑 fcd_crawler 抓一份，或用 --file 指定", file=sys.stderr)
        return 2

    blob = json.loads(path.read_text(encoding="utf-8"))
    items = blob.get("items") or []
    if args.limit:
        items = items[: args.limit]
    print(f"读入 {len(items)} 条（{blob.get('source', '')} / {blob.get('fetched_at', '')}）\n")

    results = build_results(items)

    # 与主图比对：已有同名食材的标记为「覆盖」，审核后会挂到既有节点上
    norms = [r.payload["name_norm"] for r in results if r.payload.get("name_norm")]
    in_graph = _existing_in_graph(norms)
    for r in results:
        if r.verdict in ("ok", "warn") and r.payload.get("name_norm") in in_graph:
            r.verdict = "duplicate_db"
            r.messages.append(
                importer.Message(
                    "info",
                    "图谱里已有这个食材，审核通过后会挂到既有节点上并更新营养数值",
                    "名称",
                )
            )

    print_summary(results)
    print(f"\n主图已收录同名食材：{len(in_graph)} 条（会挂到既有节点）")

    norms = [r.payload["name_norm"] for r in results if r.payload.get("name_norm")]
    merged_already = _already_merged(norms)
    if merged_already:
        print(f"本来源已导入过：{len(merged_already)} 条（跳过，重跑不会重复建边）")
        results = [r for r in results if r.payload.get("name_norm") not in merged_already]

    if args.show_error:
        print()
        for r in results:
            if r.verdict == "error":
                msgs = "；".join(m.text for m in r.messages if m.level == "error")
                print(f"  [{r.row_no}] {r.name or '（空）'} —— {msgs}")

    if args.check:
        print("\n--check：没有写库。")
        return 0

    print("\n写入审核队列…")
    totals = stage(results, path.name, args.approve)
    print(
        f"\n完成：{totals['jobs']} 个批次 / 入队 {totals['staged']} 条"
        f"（新增 {totals['created']}，覆盖 {totals['updated']}）"
        + (f"，已审核通过 {totals['approved']} 条" if args.approve else "")
    )
    if not args.approve and totals["staged"]:
        print("下一步：python -m scripts.review_bundle list   （或带 --approve 再跑一次）")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
