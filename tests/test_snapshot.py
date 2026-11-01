"""同一份内容两次保存，sha256 相同，键序不影响。"""
from __future__ import annotations

from kg.snapshot import save


def test_canonical_hash_stable(monkeypatch, tmp_path):
    monkeypatch.setattr("config.SNAPSHOT_DIR", str(tmp_path))
    uri1, sha1 = save("usda", "fdc-1", {"b": 1, "a": {"z": 2, "y": 3}})
    uri2, sha2 = save("usda", "fdc-1", {"a": {"y": 3, "z": 2}, "b": 1})
    assert sha1 == sha2
    assert uri1 != uri2 or sha1 == sha2
    assert open(uri1, "rb").read() == open(uri2, "rb").read()
