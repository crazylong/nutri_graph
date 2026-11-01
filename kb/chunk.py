"""把解析块收成可入库的块：标题回填、长文本切开、同一篇里相同图片只留一张。"""
from __future__ import annotations

import hashlib

from langchain_text_splitters import RecursiveCharacterTextSplitter

import config


def prepare(blocks: list[dict]) -> list[dict]:
    splitter = RecursiveCharacterTextSplitter(
        chunk_size=config.KB_CHUNK_CHARS,
        chunk_overlap=config.KB_CHUNK_OVERLAP,
        length_function=len,
    )
    last_heading = ""
    seen_md5: set[str] = set()
    out: list[dict] = []
    for block in blocks:
        heading = (block.get("heading_path") or "").strip() or last_heading
        if heading:
            last_heading = heading
        if block["block_kind"] == "image":
            raw = block.get("image_bytes")
            if not raw:
                name = block.get("image_name") or "image"
                note = (block.get("text") or "").strip() or f"图:{name}"
                out.append({**block, "block_kind": "text", "text": note, "heading_path": heading, "image_bytes": None})
                continue
            digest = hashlib.md5(raw).hexdigest()
            if digest in seen_md5:
                continue
            seen_md5.add(digest)
            label = (block.get("text") or "").strip() or f"图:{block.get('image_name') or digest[:8]}"
            out.append({**block, "text": label, "heading_path": heading, "image_md5": digest})
            continue
        body = (block.get("text") or "").strip()
        if not body:
            continue
        for piece in splitter.split_text(body):
            piece = piece.strip()
            if piece:
                out.append({**block, "text": piece, "heading_path": heading, "image_bytes": None})
    return out
