"""已通过的企业知识库文档整篇重嵌。

用法（在服务器上）：
  python -m scripts.reembed_kb --dry-run
  python -m scripts.reembed_kb --snapshot
  python -m scripts.reembed_kb
  python -m scripts.reembed_kb --doc-id <id>
  python -m scripts.reembed_kb --remap

--dry-run 会重新解析 PDF，不调描述、不嵌入、不删旧数据。
--snapshot 把当前 kb_chunk 写到 data/snapshots/kb_chunk_snapshot.jsonl。
--remap 用快照把 eval/kb_pairs.jsonl 的 expected_id 换成新块。对不上的题丢掉，按文档计数。
"""
from __future__ import annotations

import argparse
import json
import sys
from collections import Counter, defaultdict
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import db  # noqa: E402
from kb import parse, publish  # noqa: E402
from kb.common import KbError  # noqa: E402

SNAPSHOT = ROOT / "data" / "snapshots" / "kb_chunk_snapshot.jsonl"
PAIRS = ROOT / "eval" / "kb_pairs.jsonl"


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--doc-id", action="append", default=[])
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--snapshot", action="store_true")
    parser.add_argument("--remap", action="store_true")
    args = parser.parse_args(argv)
    if args.snapshot:
        _snapshot()
        return
    if args.remap:
        _remap()
        return
    docs = _approved(args.doc_id or None)
    if args.dry_run:
        print("dry-run 会重新解析 PDF。dots_mocr 会再跑一遍 OCR。不调描述、不嵌入、不删旧数据。")
        for doc in docs:
            _print_dry(doc)
        return
    for doc in docs:
        try:
            fresh = publish.reindex(doc["id"])
        except (KbError, Exception) as exc:  # noqa: BLE001
            print(f"失败 {doc['id']} {doc['filename']}：{exc}")
            continue
        print(f"完成 {doc['id']} {doc['filename']} 图片 {fresh.get('image_count', 0)} 张")


def _approved(doc_ids: list[str] | None) -> list[dict]:
    where = "status = 'approved'"
    args: list = []
    if doc_ids:
        where += " AND id::text = ANY(%s)"
        args.append(doc_ids)
    with db.connect() as conn:
        rows = conn.execute(
            f"""
            SELECT id::text, filename, storage_path
            FROM kb_document
            WHERE {where}
            ORDER BY created_at
            """,
            args,
        ).fetchall()
    return [{"id": row[0], "filename": row[1], "storage_path": row[2]} for row in rows]


def _print_dry(doc: dict) -> None:
    blocks = parse.parse_file(Path(doc["storage_path"]), doc["filename"])
    images = [block for block in blocks if block.get("block_kind") == "image" and block.get("image_bytes")]
    with_bbox = sum(1 for block in images if block.get("bbox"))
    print(f"{doc['id']} {doc['filename']} 图片 {len(images)} 张，有坐标 {with_bbox} 张")


def _snapshot() -> None:
    with db.connect() as conn:
        rows = conn.execute(
            """
            SELECT id, doc_id::text, block_kind, text, COALESCE(image_path, ''),
                   page_no, bbox
            FROM kb_chunk
            ORDER BY doc_id, page_no NULLS LAST, id
            """
        ).fetchall()
    SNAPSHOT.parent.mkdir(parents=True, exist_ok=True)
    lines = []
    for row in rows:
        bbox = row[6]
        if bbox is not None and not isinstance(bbox, (list, dict)):
            bbox = json.loads(bbox)
        lines.append(json.dumps({
            "id": row[0],
            "doc_id": row[1],
            "block_kind": row[2],
            "text": row[3],
            "image_path": row[4] or "",
            "page_no": row[5],
            "bbox": bbox,
        }, ensure_ascii=False))
    SNAPSHOT.write_text(("\n".join(lines) + "\n") if lines else "", encoding="utf-8")
    print(f"快照 {len(lines)} 行 -> {SNAPSHOT}")


def _remap() -> None:
    old_rows = _read_jsonl(SNAPSHOT)
    new_rows = _current_chunks()
    pairs = _read_jsonl(PAIRS)
    kept, dropped, dup_groups = remap_pairs(old_rows, new_rows, pairs)
    text = "\n".join(json.dumps(row, ensure_ascii=False) for row in kept)
    PAIRS.write_text((text + "\n") if text else "", encoding="utf-8")
    print(f"对照题 {len(pairs)} -> {len(kept)}，丢掉 {sum(dropped.values())}，重复正文 {dup_groups} 组")
    for doc_id, count in sorted(dropped.items()):
        print(f"  丢掉 {doc_id} {count} 条")


def _current_chunks() -> list[dict]:
    with db.connect() as conn:
        rows = conn.execute(
            """
            SELECT id, doc_id::text, block_kind, text, page_no, bbox
            FROM kb_chunk
            """
        ).fetchall()
    out = []
    for row in rows:
        bbox = row[5]
        if bbox is not None and not isinstance(bbox, (list, dict)):
            bbox = json.loads(bbox)
        out.append({
            "id": row[0],
            "doc_id": row[1],
            "block_kind": row[2],
            "text": row[3],
            "page_no": row[4],
            "bbox": bbox,
        })
    return out


def remap_pairs(old_rows: list[dict], new_rows: list[dict], pairs: list[dict]) -> tuple[list[dict], Counter, int]:
    """同一篇里，按页码、纵坐标、正文排序后，第 n 次出现的（类型, 正文）对上新的第 n 次。

    块 id 是随机的，不能拿来排序。对不上的题丢掉，按 doc_id 计数。
    只在解析阶段失败的文档块 id 没变，对得上，题不会被丢掉。
    """
    old_sorted = sorted(old_rows, key=_sort_key)
    new_sorted = sorted(new_rows, key=_sort_key)
    occurrence: Counter = Counter()
    slot_of: dict[str, tuple] = {}
    for row in old_sorted:
        key = (str(row.get("doc_id") or ""), row.get("block_kind") or "", row.get("text") or "")
        slot_of[row["id"]] = (key, occurrence[key], key[0])
        occurrence[key] += 1
    new_lists: dict[tuple, list[str]] = defaultdict(list)
    dup_count: Counter = Counter()
    for row in new_sorted:
        key = (str(row.get("doc_id") or ""), row.get("block_kind") or "", row.get("text") or "")
        new_lists[key].append(row["id"])
        dup_count[key] += 1
    dup_groups = sum(1 for count in dup_count.values() if count > 1)
    dropped: Counter = Counter()
    kept = []
    for pair in pairs:
        found = slot_of.get(pair.get("expected_id") or "")
        if found is None:
            dropped["unknown"] += 1
            continue
        key, slot, doc_id = found
        ids = new_lists.get(key) or []
        if slot >= len(ids):
            dropped[doc_id or "unknown"] += 1
            continue
        kept.append({**pair, "expected_id": ids[slot]})
    return kept, dropped, dup_groups


def _sort_key(row: dict) -> tuple:
    page = row.get("page_no")
    try:
        page_num = int(page) if page is not None else None
    except (TypeError, ValueError):
        page_num = None
    bbox = row.get("bbox")
    if isinstance(bbox, str):
        try:
            bbox = json.loads(bbox)
        except json.JSONDecodeError:
            bbox = None
    y = 0.0
    if isinstance(bbox, (list, tuple)) and len(bbox) >= 2:
        try:
            y = float(bbox[1] or 0)
        except (TypeError, ValueError):
            y = 0.0
    # page_no 空的排在最后，和 SQL 的 NULLS LAST 一致。
    page_key = (1, 0) if page_num is None else (0, page_num)
    return (str(row.get("doc_id") or ""), page_key[0], page_key[1], y, row.get("text") or "")


def _read_jsonl(path: Path) -> list[dict]:
    if not path.is_file():
        raise SystemExit(f"找不到 {path}")
    rows = []
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if line:
            rows.append(json.loads(line))
    return rows


if __name__ == "__main__":
    main()
