"""图片上下文、双行入库、去重、精排、重嵌对照。不连库。"""
from __future__ import annotations

import json
from pathlib import Path

import httpx
import pytest

pytestmark = pytest.mark.no_db


def _text(text: str, *, page=None, bbox=None, heading: str = "章") -> dict:
    return {
        "block_kind": "text",
        "text": text,
        "heading_path": heading,
        "page_no": page,
        "bbox": bbox,
        "image_bytes": None,
        "image_ext": "",
        "image_name": "",
    }


def _image(*, page=None, bbox=None) -> dict:
    return {
        "block_kind": "image",
        "text": "",
        "heading_path": "",
        "page_no": page,
        "bbox": bbox,
        "image_bytes": b"raw-image",
        "image_ext": ".png",
        "image_name": "figure.png",
    }


def test_caption_prompts_and_length():
    from kb.caption import _finish, _prompt

    both = _prompt("制度", "前文", "后文")
    assert both.startswith("章节：制度")
    assert "前文内容: 前文" in both and "后文内容: 后文" in both
    assert "前文内容" in _prompt("", "只有前", "")
    assert "后文内容" not in _prompt("", "只有前", "")
    assert "后文内容" in _prompt("", "", "只有后")
    assert "前文内容" not in _prompt("", "", "只有后")
    bare = _prompt("", "", "")
    assert "前文内容" not in bare and "后文内容" not in bare
    assert "300" in bare
    assert len(_finish("字" * 400, "a.png")) == 300
    assert _finish("  ", "a.png") == "图:a.png"


def test_caption_clamps_context(monkeypatch):
    import sys
    import types

    import config

    monkeypatch.setattr(config, "DASHSCOPE_API_KEY", "test-key")
    captured: dict = {}

    class _Resp:
        status_code = 200
        output = {"choices": [{"message": {"content": "一张流程图"}}]}

    fake = types.ModuleType("dashscope")

    class _Call:
        @staticmethod
        def call(**kwargs):
            captured["text"] = kwargs["messages"][0]["content"][1]["text"]
            return _Resp()

    fake.MultiModalConversation = _Call
    monkeypatch.setitem(sys.modules, "dashscope", fake)

    from kb.caption import caption_bytes

    text = caption_bytes(
        b"png", mime="image/png", heading="", filename="a.png",
        prev_text="前" * 600, next_text="后" * 600,
    )
    assert text == "一张流程图"
    assert "前" * 500 in captured["text"]
    assert "前" * 501 not in captured["text"]
    assert "后" * 500 in captured["text"]
    assert "后" * 501 not in captured["text"]


def test_chunk_context_uses_text_neighbors_and_skips_unplaced_pdf():
    from kb.chunk import prepare

    long_prev = "前" * 600
    second = _image(page=1, bbox=[0, 50, 10, 70])
    second["image_bytes"] = b"another-image"
    placed = prepare([
        _text(long_prev, page=1, bbox=[0, 0, 1, 1]),
        _image(page=1, bbox=[0, 20, 10, 40]),
        second,
        _text("后文在这里", page=1, bbox=[0, 80, 1, 90]),
    ])
    images = [block for block in placed if block["block_kind"] == "image"]
    assert images[0]["prev_text"] == "前" * 500
    assert images[0]["next_text"] == "后文在这里"
    assert images[1]["prev_text"] == "前" * 500
    assert images[1]["next_text"] == "后文在这里"

    floating = prepare([
        _text("上文", page=1, bbox=[0, 0, 1, 1]),
        _image(page=1, bbox=None),
        _text("下文", page=1, bbox=[0, 10, 1, 20]),
    ])
    image = next(block for block in floating if block["block_kind"] == "image")
    assert image["prev_text"] == "" and image["next_text"] == ""

    markdown = prepare([
        _text("上文"),
        _image(),
        _text("下文"),
    ])
    image = next(block for block in markdown if block["block_kind"] == "image")
    assert image["prev_text"] == "上文"
    assert image["next_text"] == "下文"


def test_place_page_inserts_by_y_and_scales_pixels():
    from kb.parse_pdf import _RENDER_DPI, _place_page, _point_bbox

    texts = [
        _text("上", page=1, bbox=[0, 10, 10, 20], heading="第一章"),
        _text("下", page=1, bbox=[0, 100, 10, 120], heading="第二章"),
    ]
    placed = _place_page(
        texts,
        [
            _image(page=1, bbox=[0, 50, 10, 80]),
            _image(page=1, bbox=None),
        ],
        "进入本页",
    )
    assert [block["block_kind"] for block in placed] == ["text", "image", "text", "image"]
    assert placed[1]["heading_path"] == "第一章"
    assert placed[3]["bbox"] is None and placed[3]["page_no"] == 1
    assert [block["text"] for block in placed if block["block_kind"] == "text"] == ["上", "下"]

    above = _place_page(texts, [_image(page=1, bbox=[0, 1, 2, 3])], "进入本页")
    assert above[0]["block_kind"] == "image"
    assert above[0]["heading_path"] == "进入本页"

    assert _RENDER_DPI == 144
    assert _point_bbox([144, 288, 288, 432]) == [72.0, 144.0, 144.0, 216.0]


def test_index_records_final_dir_and_two_vectors(tmp_path: Path, monkeypatch):
    from kb.publish import _index

    image = _image(page=1, bbox=[0, 1, 2, 3])
    image["prev_text"] = "前"
    image["next_text"] = "后"
    image["image_md5"] = "abc"
    monkeypatch.setattr("kb.publish.parse.parse_file", lambda path, name: [image])
    monkeypatch.setattr("kb.publish.chunk.prepare", lambda blocks, kind="": blocks)

    def caption_bytes(raw, **kwargs):
        assert kwargs["prev_text"] == "前"
        assert kwargs["next_text"] == "后"
        return "说明"

    monkeypatch.setattr("kb.publish.caption.caption_bytes", caption_bytes)
    monkeypatch.setattr("kb.publish.embed_mm.embed_text", lambda text: [0.1])
    monkeypatch.setattr("kb.publish.embed_mm.embed_image_bytes", lambda raw, mime="image/png": [0.9])
    write_dir = tmp_path / "images.new"
    record_dir = tmp_path / "images"
    rows = _index(
        {
            "id": "doc-1",
            "tenant_id": "eval",
            "filename": "a.md",
            "doc_kind": "policy",
            "storage_path": str(tmp_path / "source.md"),
        },
        [],
        write_dir=write_dir,
        record_dir=record_dir,
    )
    assert [row["block_kind"] for row in rows] == ["image", "text"]
    assert rows[0]["dense"] != rows[1]["dense"]
    assert rows[0]["text"] == rows[1]["text"] == "说明"
    assert rows[0]["image_path"] == rows[1]["image_path"]
    assert "images.new" not in rows[0]["image_path"]
    assert rows[0]["image_path"].startswith(str(record_dir))
    name = Path(rows[0]["image_path"]).name
    assert (write_dir / name).read_bytes() == b"raw-image"
    assert not (record_dir / name).exists()
    assert rows[0]["chunk_id"] != rows[1]["chunk_id"]
    assert rows[0]["image_md5"] == rows[1]["image_md5"] == "abc"


def test_match_image_boxes_by_size_when_xref_is_missing():
    from kb.parse_pdf import _match_info_boxes

    infos = [
        {"width": 60, "height": 60, "bbox": (10, 400, 20, 410)},
        {"width": 60, "height": 60, "bbox": (10, 100, 20, 110)},
        {"width": 200, "height": 80, "bbox": (30, 250, 230, 330)},
    ]
    boxes = _match_info_boxes([(60, 60), (60, 60), (200, 80)], infos, [None, None, None])
    assert [box[1] for box in boxes] == [100.0, 400.0, 250.0]


def test_varchar_fits_milvus_byte_limit():
    from kb.milvus import _varchar

    fitted = _varchar("字" * 4000, 4096)
    assert len(fitted.encode("utf-8")) <= 4096
    assert _varchar("abc", 4096) == "abc"


def test_dedupe_keeps_higher_score_and_skips_body():
    from kb.search import _dedupe

    hits = [
        {"chunk_id": "img-low", "image_path": "/a.png", "score": 0.2},
        {"chunk_id": "body-1", "image_path": "", "score": 0.5},
        {"chunk_id": "img-high", "image_path": "/a.png", "score": 0.9},
        {"chunk_id": "body-2", "image_path": "", "score": 0.1},
    ]
    assert [hit["chunk_id"] for hit in _dedupe(hits)] == ["img-high", "body-1", "body-2"]


def test_hybrid_fetch_follows_caller_limit(monkeypatch):
    import kb.milvus as milvus

    calls: dict = {}

    class _Req:
        def __init__(self, *args, **kwargs):
            self.limit = kwargs["limit"]

    class _Client:
        def hybrid_search(self, **kwargs):
            calls["limit"] = kwargs["limit"]
            calls["reqs"] = kwargs["reqs"]
            return []

        def search(self, **kwargs):
            calls["image_limit"] = kwargs["limit"]
            return []

    monkeypatch.setattr(milvus, "AnnSearchRequest", _Req)
    monkeypatch.setattr(milvus, "ensure_collection", lambda: None)
    monkeypatch.setattr(milvus, "_get_client", lambda: _Client())
    milvus.search_text("tenant", "问题", [0.1], limit=15)
    assert calls["limit"] == 30
    assert calls["reqs"][0].limit == 30
    assert calls["reqs"][1].limit == 30
    milvus.search_image("tenant", [0.1], limit=6)
    assert calls["image_limit"] == 20


def test_rerank_reorders_and_failures_cut(monkeypatch):
    import config
    from kb import rerank
    from kb.workflow import _retrieve

    hits = [
        {"chunk_id": str(i), "text": "字" * 600, "score": 1 - i / 100, "block_kind": "text", "heading_path": "", "image_path": ""}
        for i in range(8)
    ]
    monkeypatch.setattr(config, "KB_RERANK_ENABLED", True)
    monkeypatch.setattr(config, "KB_SEARCH_LIMIT", 2)
    monkeypatch.setattr(config, "KB_RERANK_CANDIDATES", 8)
    monkeypatch.setattr(config, "DASHSCOPE_API_KEY", "test-key")
    monkeypatch.setattr(config, "RERANK_MODEL_POOL", ["qwen3.7-text-rerank"])
    monkeypatch.setattr(config, "RERANK_BASE_URL", "https://example.test/rerank")

    captured: dict = {}

    class _Resp:
        status_code = 200

        def json(self):
            return {
                "output": {"results": [
                    {"index": 3, "relevance_score": 0.8},
                    {"index": 1, "relevance_score": 0.4},
                ]},
                "usage": {"total_tokens": 9},
            }

    def post(url, **kwargs):
        captured["url"] = url
        captured["body"] = kwargs["json"]
        return _Resp()

    monkeypatch.setattr(rerank.httpx, "post", post)
    ordered = rerank.rerank("差旅费", hits)
    assert [hit["chunk_id"] for hit in ordered] == ["3", "1"]
    assert ordered[0]["score"] == 0.8
    assert captured["url"] == "https://example.test/rerank"
    assert len(captured["body"]["input"]["documents"][0]) == 500
    assert captured["body"]["parameters"]["top_n"] == 2

    def boom(*args, **kwargs):
        raise httpx.TimeoutException("slow")

    monkeypatch.setattr(rerank.httpx, "post", boom)
    fallback = rerank.rerank("差旅费", hits)
    assert [hit["chunk_id"] for hit in fallback] == ["0", "1"]

    def search(tenant_id, question, limit=None):
        captured["limit"] = limit
        return hits

    monkeypatch.setattr("kb.workflow.search_question", search)

    def explode(question, rows):
        raise RuntimeError("down")

    monkeypatch.setattr(rerank, "rerank", explode)
    out = _retrieve({"tenant_id": "eval", "question": "差旅费"})
    assert captured["limit"] == 8
    assert [hit["chunk_id"] for hit in out["hits"]] == ["0", "1"]

    monkeypatch.setattr(config, "DASHSCOPE_API_KEY", "")
    called = {"n": 0}

    def refuse(*args, **kwargs):
        called["n"] += 1
        raise AssertionError("不应该发精排请求")

    monkeypatch.setattr(rerank.httpx, "post", refuse)
    monkeypatch.setattr(rerank, "rerank", lambda *args, **kwargs: (_ for _ in ()).throw(AssertionError("未调用")))
    _retrieve({"tenant_id": "eval", "question": "差旅费"})
    assert captured["limit"] == 2
    assert called["n"] == 0


def test_remap_pairs_by_order_and_counts_drops_per_doc():
    from scripts.reembed_kb import remap_pairs

    old = [
        {"id": "a1", "doc_id": "d1", "block_kind": "text", "text": "甲", "page_no": 1, "bbox": [0, 30, 1, 40]},
        {"id": "a0", "doc_id": "d1", "block_kind": "text", "text": "甲", "page_no": 1, "bbox": [0, 10, 1, 20]},
        {"id": "b1", "doc_id": "d2", "block_kind": "text", "text": "乙", "page_no": 1, "bbox": [0, 1, 1, 2]},
        {"id": "c1", "doc_id": "d1", "block_kind": "text", "text": "页码空", "page_no": None, "bbox": None},
    ]
    new = [
        {"id": "n-low", "doc_id": "d1", "block_kind": "text", "text": "甲", "page_no": 1, "bbox": [0, 10, 1, 20]},
        {"id": "n-high", "doc_id": "d1", "block_kind": "text", "text": "甲", "page_no": 1, "bbox": [0, 30, 1, 40]},
        {"id": "n-null", "doc_id": "d1", "block_kind": "text", "text": "页码空", "page_no": None, "bbox": None},
    ]
    pairs = [
        {"question": "第一处", "expected_id": "a0"},
        {"question": "第二处", "expected_id": "a1"},
        {"question": "丢了的文档", "expected_id": "b1"},
        {"question": "空页码", "expected_id": "c1"},
    ]
    kept, dropped, dup_groups = remap_pairs(old, new, pairs)
    assert [row["expected_id"] for row in kept] == ["n-low", "n-high", "n-null"]
    assert dropped["d2"] == 1
    assert "d1" not in dropped
    assert dup_groups == 1


def test_append_pairs_and_limit_counts_valid_questions(tmp_path: Path, monkeypatch):
    import scripts.gen_kb_samples as gen

    qa = tmp_path / "kb_qa.jsonl"
    pairs = tmp_path / "kb_pairs.jsonl"
    qa.write_text(json.dumps({"question": "旧题", "reference": "旧", "tenant_id": "eval"}, ensure_ascii=False) + "\n", encoding="utf-8")
    pairs.write_text(
        "\n".join([
            json.dumps({"question": "甲", "expected_id": "1"}, ensure_ascii=False),
            json.dumps({"question": "乙", "expected_id": "2"}, ensure_ascii=False),
        ]) + "\n",
        encoding="utf-8",
    )
    monkeypatch.setattr(gen, "OUT_QA", qa)
    monkeypatch.setattr(gen, "OUT_PAIRS", pairs)
    monkeypatch.setattr(gen, "primary_model", lambda **kwargs: object())
    monkeypatch.setattr(gen, "_load_chunks", lambda tenant, doc_ids=None: [
        {"chunk_id": "short", "text": "太短", "heading": ""},
        {"chunk_id": "3", "text": "足够长的正文用来出题。" * 2, "heading": ""},
        {"chunk_id": "4", "text": "另一段足够长的正文。" * 2, "heading": ""},
    ])

    def gen_one(chunk, model):
        if len((chunk["text"] or "").strip()) < 20:
            return None
        if chunk["chunk_id"] == "3":
            return {"question": "丙", "reference": "答丙"}
        return {"question": "甲", "reference": "重复"}

    monkeypatch.setattr(gen, "_gen_one", gen_one)
    gen.main(["--append", "--limit", "2", "--doc-id", "doc-1"])
    rows = [json.loads(line) for line in pairs.read_text(encoding="utf-8").splitlines() if line]
    assert [row["question"] for row in rows] == ["甲", "乙", "丙"]
    assert qa.read_text(encoding="utf-8").startswith('{"question": "旧题"')

    gen.main(["--append", "--limit", "1", "--doc-id", "doc-1"])
    rows = [json.loads(line) for line in pairs.read_text(encoding="utf-8").splitlines() if line]
    assert [row["question"] for row in rows] == ["甲", "乙", "丙"]
    assert qa.read_text(encoding="utf-8").startswith('{"question": "旧题"')
