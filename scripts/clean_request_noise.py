"""把排队里的噪音登记驳回。

默认只打印将要驳回的行。加上 --apply 才改库。

不能拿「切分结果不等于原词」当条件：停顿字「多」在「多宝鱼」里面，
那样会把真食材一起驳掉。只驳整词就是非食物，或含本次新加停顿短语的排队行。
"""
from __future__ import annotations

import argparse

import db
from kg.request import _NON_FOOD_TERMS

# 只含这次追加的短语。旧停顿字（多、吃、好）会误伤食材名，不参与存量清理。
_NEW_PHRASES = (
    "营养价值", "营养成分表", "热量表", "能不能吃",
    "每天", "每日", "一天", "每周", "早上", "晚上",
    "空腹", "饭前", "饭后", "睡前", "适量",
)


def is_noise(food_key: str) -> bool:
    if food_key in _NON_FOOD_TERMS:
        return True
    return any(phrase in food_key for phrase in _NEW_PHRASES)


def main() -> None:
    parser = argparse.ArgumentParser(description="驳回排队中的噪音登记")
    parser.add_argument("--apply", action="store_true", help="真正改库。不加则只打印")
    args = parser.parse_args()

    with db.connect() as conn:
        rows = conn.execute(
            "SELECT id, food_key FROM kg_ingredient_request "
            "WHERE status = 'queued' ORDER BY id"
        ).fetchall()
        hits = [(rid, key) for rid, key in rows if is_noise(key)]
        for rid, key in hits:
            print(f"{'驳回' if args.apply else '将驳回'} #{rid} {key}")
        print(f"共 {len(hits)} 条")
        if not args.apply or not hits:
            return
        ids = [rid for rid, _ in hits]
        conn.execute(
            "UPDATE kg_ingredient_request SET status = 'rejected', reject_reason = 'noise', "
            "handled_at = now() WHERE id = ANY(%s)",
            (ids,),
        )
        conn.execute(
            "UPDATE kg_fetch_task SET status = 'rejected' "
            "WHERE request_id = ANY(%s) AND status IN ('queued', 'failed')",
            (ids,),
        )
        with conn.cursor() as cur:
            cur.executemany(
                "INSERT INTO kg_request_action (request_id, action, payload, actor) "
                "VALUES (%s, 'ignore_noise', '{\"reason\": \"noise\"}'::jsonb, 'clean_request_noise')",
                [(rid,) for rid in ids],
            )


if __name__ == "__main__":
    main()
