"""PDF：正文和图来自 PyMuPDF。mock 只根据字号标标题，不编造正文。"""
from __future__ import annotations

from pathlib import Path

import config
from kb.common import KbError


def parse_pdf(path: Path) -> list[dict]:
    import fitz

    doc = fitz.open(path)
    try:
        if config.KB_LAYOUT_BACKEND == "dots_mocr":
            return _from_layout_service(path, doc)
        return _from_pymupdf(doc)
    finally:
        doc.close()


def _from_pymupdf(doc) -> list[dict]:
    blocks: list[dict] = []
    heading: list[str] = []
    for index, page in enumerate(doc, start=1):
        raw = page.get_text("dict")
        sizes = [
            span["size"]
            for block in raw.get("blocks") or []
            for line in block.get("lines") or []
            for span in line.get("spans") or []
            if span.get("text", "").strip()
        ]
        cutoff = (sorted(sizes)[len(sizes) // 2] * 1.15) if sizes else 99
        for block in raw.get("blocks") or []:
            if block.get("type") != 0:
                continue
            for line in block.get("lines") or []:
                spans = line.get("spans") or []
                text = "".join(span.get("text") or "" for span in spans).strip()
                if not text:
                    continue
                size = max((span.get("size") or 0) for span in spans)
                if size >= cutoff and len(text) <= 40:
                    heading = [text]
                    continue
                blocks.append({
                    "block_kind": "text",
                    "text": text,
                    "page_no": index,
                    "bbox": _bbox(line.get("bbox")),
                    "heading_path": " / ".join(heading),
                    "image_bytes": None,
                    "image_ext": "",
                    "image_name": "",
                })
        for info in page.get_images(full=True) or []:
            xref = info[0]
            try:
                extracted = doc.extract_image(xref)
            except Exception:
                continue
            if not extracted or not extracted.get("image"):
                continue
            ext = "." + (extracted.get("ext") or "png")
            blocks.append({
                "block_kind": "image",
                "text": "",
                "page_no": index,
                "bbox": _image_bbox(page, xref),
                "heading_path": " / ".join(heading),
                "image_bytes": extracted["image"],
                "image_ext": ext if ext.startswith(".") else f".{ext}",
                "image_name": f"page{index}-{xref}{ext}",
            })
    return blocks


# dots.mocr 官方推荐的版面解析 prompt（prompt_layout_all_en）：
# 输出单个 JSON，含每个布局元素的 bbox、category、text，按阅读顺序排序。
_MOCR_PROMPT = (
    "Please output the layout information from the PDF image, including each "
    "layout element's bbox, its category, and the corresponding text content "
    "within the bbox.\n"
    "1. Bbox format: [x1, y1, x2, y2]\n"
    "2. Layout Categories: The possible categories are ['Caption', 'Footnote', "
    "'Formula', 'List-item', 'Page-footer', 'Page-header', 'Picture', "
    "'Section-header', 'Table', 'Text', 'Title'].\n"
    "3. Text Extraction & Formatting Rules:\n"
    "    - Picture: For the 'Picture' category, the text field should be omitted.\n"
    "    - Formula: Format its text as LaTeX.\n"
    "    - Table: Format its text as HTML.\n"
    "    - All Others (Text, Title, etc.): Format their text as Markdown.\n"
    "4. Constraints:\n"
    "    - The output text must be the original text from the image, with no translation.\n"
    "    - All layout elements must be sorted according to human reading order.\n"
    "5. Final Output: The entire output must be a single JSON object."
)

# 参与标题链（heading_path）的类别；层级按 Title > Section-header 粗分。
_HEADING_KINDS = {"Title", "Section-header"}
# 作为正文落块的类别；Picture 由 PyMuPDF 抽，页眉页脚是噪音直接跳过。
_TEXT_KINDS = {"Text", "List-item", "Caption", "Footnote", "Table", "Formula"}
_SKIP_KINDS = {"Picture", "Page-header", "Page-footer"}


def _from_layout_service(path: Path, doc) -> list[dict]:
    if not config.KB_LAYOUT_URL:
        raise KbError("KB_LAYOUT_BACKEND=dots_mocr 但没有 KB_LAYOUT_URL", code="no_layout")
    import base64

    import httpx

    base_url = config.KB_LAYOUT_URL.rstrip("/")
    # dots.mocr 是图像 OCR 模型，PDF 先按页渲染成 PNG 再逐页喂给它
    blocks: list[dict] = []
    for number, page in enumerate(doc, start=1):
        png = page.get_pixmap(dpi=144)
        image_b64 = base64.b64encode(png.tobytes("png")).decode("ascii")

        headers = {"Content-Type": "application/json"}
        if config.KB_LAYOUT_API_KEY:
            headers["Authorization"] = f"Bearer {config.KB_LAYOUT_API_KEY}"
        payload = {
            "model": config.KB_LAYOUT_MODEL,
            "messages": [
                {
                    "role": "user",
                    "content": [
                        {
                            "type": "image_url",
                            "image_url": {"url": f"data:image/png;base64,{image_b64}"},
                        },
                        {"type": "text", "text": _MOCR_PROMPT},
                    ],
                }
            ],
            "temperature": 0.1,
            "max_tokens": 4096,
        }
        response = httpx.post(
            base_url + "/chat/completions",
            json=payload,
            headers=headers,
            timeout=180,
        )
        if response.status_code != 200:
            raise KbError(
                f"版面解析失败：第 {number} 页 HTTP {response.status_code}",
                status=502,
                code="layout_failed",
            )
        content = _extract_mocr_text(response.json())
        blocks.extend(_blocks_from_layout_json(content, number))

    images = _from_pymupdf(doc)
    blocks.extend(block for block in images if block["block_kind"] == "image")
    return blocks


def _extract_mocr_text(data: dict) -> str:
    try:
        return data["choices"][0]["message"]["content"]
    except (KeyError, IndexError, TypeError):
        raise KbError("版面解析返回结构异常，缺少 choices[0].message.content", status=502, code="layout_failed")


def _blocks_from_layout_json(content: str, page_no: int) -> list[dict]:
    """把 dots.mocr 的 JSON 版面解析结果还原成统一 block。"""
    items = _parse_layout_items(content)
    blocks: list[dict] = []
    heading: list[str] = []
    for item in items:
        if not isinstance(item, dict):
            continue
        kind = str(item.get("category") or item.get("type") or "Text")
        text = (item.get("text") or "").strip()
        bbox = item.get("bbox")
        if kind in _HEADING_KINDS and text:
            # 官方 prompt 让标题走 Markdown，可能带 # 前缀，剥掉纯标题文本
            clean = _strip_markdown_markers(text)
            heading = heading[:1] + [clean] if kind == "Title" else [clean]
            continue
        if kind in _SKIP_KINDS:
            continue
        if kind not in _TEXT_KINDS or not text:
            continue
        blocks.append({
            "block_kind": "text",
            "text": text,
            "page_no": page_no,
            "bbox": _bbox(bbox),
            "heading_path": " / ".join(heading),
            "image_bytes": None,
            "image_ext": "",
            "image_name": "",
        })
    return blocks


def _strip_markdown_markers(text: str) -> str:
    """剥掉标题文本里的 Markdown 标记号（如 '## 第一章' → '第一章'）。"""
    import re

    stripped = re.sub(r"^#{1,6}\s+", "", text)
    stripped = re.sub(r"\s+#{1,6}$", "", stripped)
    return stripped.strip()


def _parse_layout_items(content: str) -> list[dict]:
    """容错解析 dots.mocr 输出的 JSON。模型可能直接给数组，也可能包在
    {objects|items|layout} 等键下，或带 ```json 围栏。失败时退化成单块正文。"""
    import json
    import re

    text = (content or "").strip()
    if text.startswith("```"):
        text = re.sub(r"^```[a-zA-Z]*\s*", "", text)
        text = re.sub(r"\s*```$", "", text)
    try:
        data = json.loads(text)
    except json.JSONDecodeError:
        # 模型没给纯 JSON 时，兜底：整段当正文（至少文字还在，不丢内容）。
        return [{"category": "Text", "text": content}]

    if isinstance(data, list):
        return data
    if isinstance(data, dict):
        for key in ("objects", "items", "layout", "elements", "blocks", "results"):
            if isinstance(data.get(key), list):
                return data[key]
        return [data]
    return [{"category": "Text", "text": content}]


def _bbox(value) -> list[float] | None:
    if not value or len(value) < 4:
        return None
    return [float(value[0]), float(value[1]), float(value[2]), float(value[3])]


def _image_bbox(page, xref: int) -> list[float] | None:
    try:
        for info in page.get_image_info() or []:
            if info.get("xref") == xref and info.get("bbox"):
                return _bbox(info["bbox"])
    except Exception:
        return None
    return None
