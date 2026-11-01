"""按扩展名选解析器。"""
from __future__ import annotations

from pathlib import Path

from kb.common import KbError
from kb.parse_docx import parse_docx
from kb.parse_md import parse_markdown
from kb.parse_pdf import parse_pdf


def parse_file(path: Path, filename: str) -> list[dict]:
    suffix = Path(filename).suffix.lower()
    if suffix in {".md", ".markdown"}:
        return parse_markdown(path)
    if suffix == ".pdf":
        return parse_pdf(path)
    if suffix == ".docx":
        return parse_docx(path)
    raise KbError("只接收 md、pdf、docx", code="bad_file")
