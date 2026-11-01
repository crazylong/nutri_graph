"""来源快照。同一份内容两次保存，sha256 必须相同。"""
from __future__ import annotations

import hashlib
import json
import re
from pathlib import Path

import config

_SAFE = re.compile(r"[^0-9A-Za-z._-]+")


def save(source: str, external_id: str, raw) -> tuple[str, str]:
    if isinstance(raw, (bytes, bytearray)):
        body = bytes(raw)
    else:
        body = json.dumps(raw, sort_keys=True, ensure_ascii=False, default=str).encode("utf-8")
    digest = hashlib.sha256(body).hexdigest()
    folder = Path(config.SNAPSHOT_DIR)
    if not folder.is_absolute():
        folder = Path(__file__).resolve().parents[1] / folder
    name = _SAFE.sub("_", f"{external_id}-{digest[:12]}") or digest[:12]
    path = (folder / _SAFE.sub("_", source or "raw") / f"{name}.json").resolve()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(body)
    return str(path), digest
