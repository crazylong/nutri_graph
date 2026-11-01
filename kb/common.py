"""企业知识库的共用小工具。"""
from __future__ import annotations

import re
from pathlib import Path

import config

TENANT_RE = re.compile(r"^[A-Za-z0-9_]{1,32}$")
DOC_KINDS = {"tech": "技术类", "policy": "制度类"}
ABSTAIN = "这批制度里没有这段"
IMAGE_WARN = 5 * 1024 * 1024
IMAGE_MAX = 10 * 1024 * 1024


class KbError(Exception):
    def __init__(self, message: str, *, status: int = 400, code: str = "kb_error"):
        super().__init__(message)
        self.message = message
        self.status = status
        self.code = code


def check_tenant(value: str) -> str:
    text = (value or "").strip()
    if not TENANT_RE.fullmatch(text):
        raise KbError("租户只允许字母、数字、下划线，最长 32 位", code="bad_tenant")
    return text


def check_kind(value: str) -> str:
    text = (value or "").strip()
    if text not in DOC_KINDS:
        raise KbError("分类只能是技术类或制度类", code="bad_kind")
    return text


def format_hit(hit: dict) -> str:
    """回答和离线评估看到的同一段依据：章节路径加正文。"""
    heading = (hit.get("heading_path") or "").strip() or "正文"
    return f"[{heading}] {hit.get('text') or ''}"


def data_root() -> Path:
    root = Path(config.KB_DATA_DIR)
    root.mkdir(parents=True, exist_ok=True)
    return root


def doc_dir(doc_id: str) -> Path:
    path = data_root() / doc_id
    path.mkdir(parents=True, exist_ok=True)
    return path
