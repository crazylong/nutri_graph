"""解析不连库。mock 只标标题，正文必须来自文件本身。"""
from __future__ import annotations

from pathlib import Path

import pytest

from kb.chunk import prepare
from kb.common import format_hit
from kb.parse_docx import parse_docx
from kb.parse_md import parse_markdown
from kb.parse_pdf import parse_pdf

pytestmark = pytest.mark.no_db


def test_hit_text_keeps_heading_for_answer_and_eval():
    assert format_hit({"heading_path": "制度 / 报销", "text": "差旅费按票据实报。"}) == "[制度 / 报销] 差旅费按票据实报。"
    assert format_hit({"text": "只有正文"}) == "[正文] 只有正文"


def test_recall_scores_include_first_hit_and_mrr():
    from eval.kb_run import score_ranks

    scores = score_ranks([1, 3, None])
    assert scores["n"] == 3
    assert scores["recall_at_1"] == 1 / 3
    assert scores["recall_at_5"] == 2 / 3
    assert scores["recall_at_10"] == 2 / 3
    assert scores["mrr"] == pytest.approx((1 + 1 / 3) / 3)


def test_markdown_heading_chain(tmp_path: Path):
    path = tmp_path / "rule.md"
    path.write_text("# 制度\n## 报销\n差旅费按票据实报。\n", encoding="utf-8")
    blocks = parse_markdown(path)
    assert blocks[0]["heading_path"] == "制度 / 报销"
    assert "差旅费" in blocks[0]["text"]
    assert blocks[0]["bbox"] is None


def test_markdown_remote_image_is_a_note(tmp_path: Path):
    path = tmp_path / "rule.md"
    path.write_text("# 章\n![架构](https://example.com/a.png)\n", encoding="utf-8")
    blocks = prepare(parse_markdown(path))
    assert blocks[0]["block_kind"] == "text"
    assert "架构" in blocks[0]["text"]
    assert blocks[0]["image_bytes"] is None


def test_docx_has_heading_and_empty_bbox(tmp_path: Path):
    docx = pytest.importorskip("docx")
    path = tmp_path / "rule.docx"
    document = docx.Document()
    document.add_heading("报销制度", level=1)
    document.add_paragraph("差旅费按票据实报。")
    document.save(path)
    blocks = parse_docx(path)
    assert blocks[0]["heading_path"] == "报销制度"
    assert "差旅费" in blocks[0]["text"]
    assert blocks[0]["page_no"] is None
    assert blocks[0]["bbox"] is None


def test_pdf_body_comes_from_the_file(tmp_path: Path):
    fitz = pytest.importorskip("fitz")
    path = tmp_path / "rule.pdf"
    doc = fitz.open()
    page = doc.new_page()
    page.insert_text((72, 80), "报销制度", fontname="china-s", fontsize=22)
    page.insert_text((72, 120), "差旅费按票据实报。", fontname="china-s", fontsize=12)
    doc.save(path)
    doc.close()
    blocks = parse_pdf(path)
    text = "\n".join(block["text"] for block in blocks)
    assert "差旅费按票据实报" in text
    assert any(block["page_no"] == 1 for block in blocks)


def _text_block(text: str, heading: str = "") -> dict:
    return {
        "block_kind": "text",
        "text": text,
        "heading_path": heading,
        "page_no": None,
        "bbox": None,
        "image_bytes": None,
        "image_ext": "",
        "image_name": "",
    }


def test_qa_kind_keeps_question_and_answer_in_one_chunk():
    body = "Q: 差旅费怎么报销？\nA: 登录 OA 提交单据。\n问：多久到账？\n答：三个工作日。"
    blocks = prepare([_text_block(body)], "qa")
    assert len(blocks) == 2
    assert blocks[0]["text"].startswith("Q: 差旅费怎么报销？")
    assert "登录 OA 提交单据" in blocks[0]["text"]
    assert blocks[1]["text"].startswith("问：多久到账？")
    assert "三个工作日" in blocks[1]["text"]


def test_qa_kind_prefixes_question_heading():
    blocks = prepare([_text_block("差旅费按票据实报。", "制度 / 如何报销差旅费？")], "qa")
    assert blocks[0]["text"] == "问：如何报销差旅费？\n差旅费按票据实报。"
    assert blocks[0]["heading_path"] == "制度 / 如何报销差旅费？"


def test_non_qa_kind_does_not_split_pairs():
    body = "Q: 差旅费怎么报销？\nA: 登录 OA 提交单据。"
    blocks = prepare([_text_block(body)], "policy")
    assert len(blocks) == 1
    assert blocks[0]["text"] == body


def test_qa_long_answer_splits_but_repeats_question(monkeypatch):
    monkeypatch.setattr("config.KB_CHUNK_CHARS", 40)
    monkeypatch.setattr("config.KB_CHUNK_OVERLAP", 8)
    body = "Q: 年假怎么算？\n" + "入职满一年享受五天年假。" * 6
    blocks = prepare([_text_block(body)], "qa")
    assert len(blocks) > 1
    assert all(block["text"].startswith("Q: 年假怎么算？") for block in blocks)


def test_check_kind_accepts_qa():
    from kb.common import check_kind

    assert check_kind("qa") == "qa"
    with pytest.raises(Exception):
        check_kind("faq")
