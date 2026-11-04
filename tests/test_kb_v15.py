"""收敛之后按序号补同一章节的前后块。拼接不连库。"""
from __future__ import annotations

import pytest

pytestmark = pytest.mark.no_db


def _block(chunk_id: str, heading: str = "差旅", image: str = "", **extra) -> dict:
    row = {
        "chunk_id": chunk_id,
        "text": chunk_id,
        "heading_path": heading,
        "image_path": image,
        "block_kind": "text",
        "score": 0.5,
    }
    row.update(extra)
    return row


def test_prev_and_next_sit_beside_the_hit():
    from kb.neighbor import attach

    hit = _block("b")
    neighbors = {"b": {"prev": _block("a"), "next": _block("c")}}
    out = attach([hit], neighbors)
    assert [row["chunk_id"] for row in out] == ["a", "b", "c"]
    assert out[0]["neighbor"] is True and "score" not in out[0]
    assert out[2]["neighbor"] is True and "score" not in out[2]
    assert "neighbor" not in out[1]
    assert out[1]["score"] == 0.5


def test_different_heading_is_not_attached():
    from kb.neighbor import attach

    hit = _block("b", heading="住宿")
    neighbors = {"b": {"next": _block("c", heading="交通")}}
    assert [row["chunk_id"] for row in attach([hit], neighbors)] == ["b"]


def test_same_image_path_is_not_attached():
    from kb.neighbor import attach

    hit = _block("img", image="/a.png")
    neighbors = {"img": {"next": _block("cap", image="/a.png")}}
    assert [row["chunk_id"] for row in attach([hit], neighbors)] == ["img"]


def test_adjacent_hits_keep_the_shared_block_once_and_unmarked():
    from kb.neighbor import attach

    first, second = _block("a"), _block("b")
    neighbors = {
        "a": {"next": _block("b")},
        "b": {"prev": _block("a")},
    }
    out = attach([first, second], neighbors)
    assert [row["chunk_id"] for row in out] == ["a", "b"]
    assert all("neighbor" not in row for row in out)


def test_edges_omit_the_missing_side():
    from kb.neighbor import attach

    out = attach([_block("only")], {"only": {}})
    assert [row["chunk_id"] for row in out] == ["only"]


def test_missing_mapping_returns_the_hit():
    from kb.neighbor import attach

    hit = _block("b")
    assert attach([hit], {}) == [hit]


def test_neighbor_switch_off_skips_the_lookup(monkeypatch):
    import config
    from kb.workflow import _retrieve

    monkeypatch.setattr(config, "KB_NEIGHBOR_ENABLED", False)
    monkeypatch.setattr(config, "KB_RERANK_ENABLED", False)
    monkeypatch.setattr(config, "KB_SEARCH_LIMIT", 2)
    rows = [_block(str(i), heading=str(i)) for i in range(4)]

    def explode(*args, **kwargs):
        raise AssertionError("开关关掉不应读邻居")

    monkeypatch.setattr("kb.workflow.search_question", lambda *args, **kwargs: rows)
    monkeypatch.setattr("kb.workflow.store.neighbors_of", explode)
    out = _retrieve({"tenant_id": "eval", "question": "差旅"})
    assert [hit["chunk_id"] for hit in out["hits"]] == ["0", "1"]


def test_lookup_failure_keeps_diversified_hits(monkeypatch):
    import config
    from kb.workflow import _retrieve

    monkeypatch.setattr(config, "KB_NEIGHBOR_ENABLED", True)
    monkeypatch.setattr(config, "KB_RERANK_ENABLED", False)
    monkeypatch.setattr(config, "KB_SEARCH_LIMIT", 2)
    rows = [_block(str(i), heading=str(i)) for i in range(4)]

    def boom(hits):
        raise RuntimeError("db down")

    monkeypatch.setattr("kb.workflow.search_question", lambda *args, **kwargs: rows)
    monkeypatch.setattr("kb.workflow.store.neighbors_of", boom)
    out = _retrieve({"tenant_id": "eval", "question": "差旅"})
    assert [hit["chunk_id"] for hit in out["hits"]] == ["0", "1"]


def test_run_ask_accepts_neighbor_without_score(monkeypatch):
    from kb.workflow import run_ask

    class _Graph:
        def invoke(self, state):
            return {
                "answer": "按票据。",
                "abstained": False,
                "hits": [{
                    "chunk_id": "next",
                    "block_kind": "text",
                    "text": "以上不适用于实习生",
                    "heading_path": "差旅",
                    "image_path": "",
                    "neighbor": True,
                }],
            }

    monkeypatch.setattr("kb.workflow._graph", lambda: _Graph())
    out = run_ask(tenant_id="eval", question="实习生能报吗")
    assert out["hits"][0]["score"] is None
    assert out["hits"][0]["neighbor"] is True
    assert out["hits"][0]["text"] == "以上不适用于实习生"
