"""提问原图的文件名校验。不连库。"""
from __future__ import annotations

import pytest

pytestmark = pytest.mark.no_db


def test_ask_image_roundtrip_and_rejects_escape(tmp_path, monkeypatch):
    monkeypatch.setattr("config.KB_DATA_DIR", str(tmp_path))
    from kb import memory

    session_id = "11111111-1111-1111-1111-111111111111"
    name = memory.save_ask_image(session_id, b"png-bytes", "image/jpeg")
    assert name.endswith(".jpg")
    assert memory.ask_image_path(session_id, name).read_bytes() == b"png-bytes"
    assert memory.ask_image_path(session_id, "../" + name) is None
    assert memory.ask_image_path(session_id, name + ".txt") is None
    assert memory.ask_image_path("not-a-uuid", name) is None
    assert memory.ask_image_path(session_id, "0" * 32 + ".png") is None
