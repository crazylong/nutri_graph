"""
scripts/index_entities.py —— 把图谱实体批量索引进 Milvus（向量兜底）

为什么需要：实体链接的向量兜底（EntityLinker.vector_search）靠 Milvus 的 kg_entity
集合，但它是空的 —— 没有这个脚本，种子 125 个食材 + 营养素 + 人群/症状/禁忌
永远只能靠字典精确/模糊匹配，向量那一级形同虚设。

数据来源：dict_alias 表（就是实体链接的主表），按 canonical_id 去重，
每个实体用「name + 别名拼接」的文本生成 embedding。

用法：
  python -m scripts.index_entities            # 全量重建（先清空 kg_entity 再写）
  python -m scripts.index_entities --dry-run  # 只看会索引哪些，不写

注意：provisional 节点**不**索引进 Milvus —— 见 kg/promote.py 的说明，
这里从 dict_alias 读，provisional 节点本就不会出现在 dict_alias（它只在 kg_node_candidate）。
"""
from __future__ import annotations

import argparse

import psycopg

import config


def collect_entities() -> list[dict]:
    """从 dict_alias 汇总每个 canonical_id 的唯一实体，带别名列表。"""
    with psycopg.connect(config.PG_DSN) as conn:
        cur = conn.cursor()
        # dict_alias 没有 id 列；name 取该实体第一个出现的别名即可。
        cur.execute(
            "SELECT canonical_id, entity_type, alias FROM dict_alias "
            "ORDER BY canonical_id, created_at"
        )
        rows = cur.fetchall()

    # 按 canonical_id 聚合
    entities: dict[str, dict] = {}
    for canonical_id, entity_type, alias in rows:
        if canonical_id not in entities:
            entities[canonical_id] = {
                "canonical_id": canonical_id,
                "label": entity_type,
                "name": alias,          # 第一个别名作为 name
                "aliases": [],
            }
        if alias not in entities[canonical_id]["aliases"]:
            entities[canonical_id]["aliases"].append(alias)
    return list(entities.values())


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry-run", action="store_true", help="只看不写")
    ap.add_argument("--limit", type=int, default=0, help="只索引前 N 个（调试用）")
    args = ap.parse_args()

    entities = collect_entities()
    if args.limit:
        entities = entities[: args.limit]

    if args.dry_run:
        print(f"待索引 {len(entities)} 个实体：")
        for e in entities[:20]:
            print(f"  {e['label']:<12} {e['canonical_id']:<16} {e['name']}")
        if len(entities) > 20:
            print(f"  … 共 {len(entities)} 个")
        return 0

    from rag.client import insert_entity
    from rag.embed import get_embeddings

    emb = get_embeddings()
    done = 0
    for e in entities:
        text = " ".join([e["name"], *e["aliases"]])
        vec = emb.embed_query(text)
        insert_entity(
            canonical_id=e["canonical_id"],
            label=e["label"],
            name=e["name"],
            text=text,
            embedding=vec,
        )
        done += 1
        if done % 20 == 0:
            print(f"  已索引 {done}/{len(entities)}")

    print(f"完成：{done} 个实体已写入 Milvus kg_entity")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
