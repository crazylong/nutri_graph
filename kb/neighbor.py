"""把收敛后的命中和已读出的邻居拼成前一块、命中、后一块。不查库。"""
from __future__ import annotations


def attach(hits: list[dict], neighbors: dict[str, dict]) -> list[dict]:
    """邻居不带检索分。本身也是命中的块只在命中的位置出现一次。"""
    rows = list(hits or [])
    hit_ids = {hit.get("chunk_id") for hit in rows if hit.get("chunk_id")}
    out: list[dict] = []
    seen: set[str] = set()
    for hit in rows:
        pair = neighbors.get(hit.get("chunk_id")) or {}
        _place(out, seen, hit_ids, hit, pair.get("prev"), as_neighbor=True)
        _place(out, seen, hit_ids, hit, hit, as_neighbor=False)
        _place(out, seen, hit_ids, hit, pair.get("next"), as_neighbor=True)
    return out


def _place(out: list[dict], seen: set[str], hit_ids: set, anchor: dict, block: dict | None, *, as_neighbor: bool) -> None:
    if not block:
        return
    chunk_id = block.get("chunk_id")
    if not chunk_id or chunk_id in seen:
        return
    if as_neighbor:
        if chunk_id in hit_ids or not _fits(anchor, block):
            return
        item = dict(block)
        item["neighbor"] = True
        item.pop("score", None)
        out.append(item)
    else:
        out.append(block)
    seen.add(chunk_id)


def _fits(anchor: dict, block: dict) -> bool:
    if (block.get("heading_path") or "") != (anchor.get("heading_path") or ""):
        return False
    path = (anchor.get("image_path") or "").strip()
    other = (block.get("image_path") or "").strip()
    return not (path and path == other)
