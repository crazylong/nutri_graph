"""近义正文收成一条。不连库。"""
from __future__ import annotations

import pytest

pytestmark = pytest.mark.no_db

_COPY = "差旅住宿标准按城市级别执行报销细则说明如下"


def _unique(index: int, prefix: str) -> dict:
    base = 0x5000 + index * 24
    return {
        "chunk_id": f"{prefix}{index}",
        "text": "".join(chr(base + i) for i in range(16)),
        "heading_path": "同一章",
        "block_kind": "text",
        "image_path": "",
        "score": 1,
    }


def test_similar_cluster_keeps_first_and_backfills():
    from kb.diversify import diversify

    hits = [{"chunk_id": f"c{i}", "text": _COPY, "heading_path": "章"} for i in range(5)]
    hits.extend(_unique(i, "u") for i in range(15))
    kept = diversify(hits, limit=6, threshold=0.72)
    assert [hit["chunk_id"] for hit in kept] == ["c0", "u0", "u1", "u2", "u3", "u4"]


def test_shortfall_does_not_restore_dropped_copies():
    from kb.diversify import diversify

    hits = [{"chunk_id": f"c{i}", "text": _COPY, "heading_path": "章"} for i in range(5)]
    hits.extend(_unique(i, "u") for i in range(3))
    kept = diversify(hits, limit=6, threshold=0.72)
    assert [hit["chunk_id"] for hit in kept] == ["c0", "u0", "u1", "u2"]


def test_same_heading_with_different_words_both_stay():
    from kb.diversify import diversify

    hits = [_unique(0, "a"), _unique(1, "b")]
    hits[0]["heading_path"] = hits[1]["heading_path"] = "1.1 差旅"
    assert len(diversify(hits, limit=6, threshold=0.72)) == 2


def test_same_wording_different_numbers_both_stay():
    from kb.diversify import diversify

    low = "市内交通费：一线城市每人每天 100 元，二线城市每人每天 80 元。"
    high = "市内交通费：一线城市每人每天 120 元，二线城市每人每天 90 元。"
    same = "市内交通费：一线城市每人每天100元，二线城市每人每天80元。"
    hits = [
        {"chunk_id": "low", "text": low},
        {"chunk_id": "high", "text": high},
        {"chunk_id": "same", "text": same},
    ]
    assert [hit["chunk_id"] for hit in diversify(hits, limit=6, threshold=0.72)] == ["low", "high"]


def test_newline_and_fullwidth_digits_collapse():
    from kb.diversify import diversify

    hits = [
        {"chunk_id": "wrap", "text": "差旅费\n报销标准说明全文"},
        {"chunk_id": "flat", "text": "差旅费报销标准说明全文"},
        {"chunk_id": "wide", "text": "每天１００元整的交通补贴办法"},
        {"chunk_id": "narrow", "text": "每天100元整的交通补贴办法"},
    ]
    assert [hit["chunk_id"] for hit in diversify(hits, limit=6, threshold=0.72)] == ["wrap", "wide"]


def test_empty_text_keeps_one():
    from kb.diversify import diversify

    hits = [{"chunk_id": str(i), "text": ""} for i in range(3)]
    assert [hit["chunk_id"] for hit in diversify(hits, limit=6, threshold=0.72)] == ["0"]


def test_threshold_above_one_only_cuts_to_limit():
    from kb.diversify import diversify

    hits = [_unique(i, "u") for i in range(10)]
    kept = diversify(hits, limit=6, threshold=2)
    assert [hit["chunk_id"] for hit in kept] == [f"u{i}" for i in range(6)]


def test_diversify_off_cuts_to_search_limit(monkeypatch):
    import config
    from kb.workflow import _retrieve

    monkeypatch.setattr(config, "KB_DIVERSIFY_ENABLED", False)
    monkeypatch.setattr(config, "KB_RERANK_ENABLED", False)
    monkeypatch.setattr(config, "KB_SEARCH_LIMIT", 2)
    rows = [_unique(i, "u") for i in range(5)]
    monkeypatch.setattr("kb.workflow.search_question", lambda *args, **kwargs: rows)
    out = _retrieve({"tenant_id": "eval", "question": "差旅"})
    assert [hit["chunk_id"] for hit in out["hits"]] == ["u0", "u1"]


def test_bad_text_falls_back_to_prefix(monkeypatch):
    import config
    from kb.workflow import _retrieve

    monkeypatch.setattr(config, "KB_RERANK_ENABLED", False)
    monkeypatch.setattr(config, "KB_SEARCH_LIMIT", 2)
    rows = [
        {"chunk_id": "0", "text": ["不是字符串"], "score": 1, "block_kind": "text", "heading_path": "", "image_path": ""},
        _unique(1, "u"),
        _unique(2, "u"),
    ]
    monkeypatch.setattr("kb.workflow.search_question", lambda *args, **kwargs: rows)
    out = _retrieve({"tenant_id": "eval", "question": "差旅"})
    assert [hit["chunk_id"] for hit in out["hits"]] == ["0", "u1"]
