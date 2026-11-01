"""Markdown：按标题切，抽出图片语法。文件里没有的图不编造字节。"""
from __future__ import annotations

import re
from pathlib import Path

_HEADING = re.compile(r"^(#{1,6})\s+(.*?)\s*$")
_IMAGE = re.compile(r"!\[([^\]]*)\]\(([^)]+)\)")


def parse_markdown(path: Path) -> list[dict]:
    text = path.read_text(encoding="utf-8", errors="replace")
    heading: list[str] = []
    buf: list[str] = []
    blocks: list[dict] = []

    def flush() -> None:
        body = "\n".join(buf).strip()
        buf.clear()
        if body:
            blocks.append(_text(body, heading))

    for line in text.splitlines():
        found = _HEADING.match(line)
        if found:
            flush()
            level = len(found.group(1))
            heading = heading[: level - 1] + [found.group(2).strip()]
            continue
        images = list(_IMAGE.finditer(line))
        if images:
            flush()
            for image in images:
                blocks.append(_image(path, image.group(1), image.group(2), heading))
            rest = _IMAGE.sub("", line).strip()
            if rest:
                buf.append(rest)
            continue
        buf.append(line)
    flush()
    return blocks


def _text(body: str, heading: list[str]) -> dict:
    return {
        "block_kind": "text",
        "text": body,
        "page_no": None,
        "bbox": None,
        "heading_path": " / ".join(heading),
        "image_bytes": None,
        "image_ext": "",
        "image_name": "",
    }


def _image(md_path: Path, alt: str, target: str, heading: list[str]) -> dict:
    name = Path(target).name or (alt or "image")
    raw = _local_bytes(md_path, target)
    return {
        "block_kind": "image",
        "text": alt.strip(),
        "page_no": None,
        "bbox": None,
        "heading_path": " / ".join(heading),
        "image_bytes": raw,
        "image_ext": Path(name).suffix.lower() or ".png",
        "image_name": name,
    }


def _local_bytes(md_path: Path, target: str) -> bytes | None:
    if "://" in target or target.startswith("data:"):
        return None
    candidate = (md_path.parent / target).resolve()
    if candidate.is_file() and md_path.parent.resolve() in candidate.parents:
        return candidate.read_bytes()
    return None
