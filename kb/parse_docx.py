"""DOCX：按 Heading 样式做标题链，抽出段落和内嵌图。页码和坐标留空。"""
from __future__ import annotations

from pathlib import Path

_BLIP = "{http://schemas.openxmlformats.org/drawingml/2006/main}blip"
_EMBED = "{http://schemas.openxmlformats.org/officeDocument/2006/relationships}embed"


def parse_docx(path: Path) -> list[dict]:
    from docx import Document

    doc = Document(str(path))
    heading: list[str] = []
    blocks: list[dict] = []
    seen: set[str] = set()
    for paragraph in doc.paragraphs:
        style = paragraph.style.name if paragraph.style is not None else ""
        text = paragraph.text.strip()
        level = _heading_level(style)
        if level and text:
            heading = heading[: level - 1] + [text]
            continue
        if text:
            blocks.append({
                "block_kind": "text",
                "text": text,
                "page_no": None,
                "bbox": None,
                "heading_path": " / ".join(heading),
                "image_bytes": None,
                "image_ext": "",
                "image_name": "",
            })
        for raw, ext, name in _images(paragraph, doc):
            if name in seen:
                continue
            seen.add(name)
            blocks.append({
                "block_kind": "image",
                "text": "",
                "page_no": None,
                "bbox": None,
                "heading_path": " / ".join(heading),
                "image_bytes": raw,
                "image_ext": ext,
                "image_name": name,
            })
    return blocks


def _heading_level(style: str) -> int:
    if not style.startswith("Heading"):
        return 0
    digits = "".join(ch for ch in style if ch.isdigit())
    return int(digits or "1")


def _images(paragraph, doc) -> list[tuple[bytes, str, str]]:
    out = []
    blips = list(paragraph._element.iter(_BLIP))
    for blip in blips:
        embed = blip.get(_EMBED)
        part = doc.part.related_parts.get(embed) if embed else None
        if part is None:
            continue
        name = Path(part.partname).name
        ext = Path(name).suffix.lower() or ".png"
        out.append((part.blob, ext, name))
    return out
