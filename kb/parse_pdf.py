"""PDF：正文和图来自 PyMuPDF。mock 只根据字号标标题，不编造正文。

版面服务返回的框是渲染像素，PyMuPDF 的图框是 PDF 点。写入块之前把像素换成点，
两套坐标才能比上下。渲染 dpi 只用 _RENDER_DPI 这一处。
"""
from __future__ import annotations

from pathlib import Path

import config
from kb.common import KbError

# 渲染给版面模型看的分辨率。换算回 PDF 点时用 72 / 这个值，不要再写死 144。
_RENDER_DPI = 144


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
        start_heading = " / ".join(heading)
        texts, heading = _page_texts(page, index, heading)
        images = _page_images(doc, page, index)
        blocks.extend(_place_page(texts, images, start_heading))
    return blocks


def _page_texts(page, page_no: int, heading: list[str]) -> tuple[list[dict], list[str]]:
    raw = page.get_text("dict")
    sizes = [
        span["size"]
        for block in raw.get("blocks") or []
        for line in block.get("lines") or []
        for span in line.get("spans") or []
        if span.get("text", "").strip()
    ]
    cutoff = (sorted(sizes)[len(sizes) // 2] * 1.15) if sizes else 99
    texts: list[dict] = []
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
            texts.append({
                "block_kind": "text",
                "text": text,
                "page_no": page_no,
                "bbox": _bbox(line.get("bbox")),
                "heading_path": " / ".join(heading),
                "image_bytes": None,
                "image_ext": "",
                "image_name": "",
            })
    return texts, heading


def _page_images(doc, page, page_no: int) -> list[dict]:
    metas = []
    extracted_rows = []
    for info in page.get_images(full=True) or []:
        xref = info[0]
        try:
            extracted = doc.extract_image(xref)
        except Exception:
            continue
        if not extracted or not extracted.get("image"):
            continue
        ext = "." + (extracted.get("ext") or "png")
        if not ext.startswith("."):
            ext = f".{ext}"
        width = info[2] if len(info) > 3 else 0
        height = info[3] if len(info) > 3 else 0
        metas.append((xref, width, height))
        extracted_rows.append((extracted, ext, xref))
    boxes = _boxes_for_page(page, metas)
    images = []
    for (extracted, ext, xref), bbox in zip(extracted_rows, boxes):
        images.append({
            "block_kind": "image",
            "text": "",
            "page_no": page_no,
            "bbox": bbox,
            "heading_path": "",
            "image_bytes": extracted["image"],
            "image_ext": ext,
            "image_name": f"page{page_no}-{xref}{ext}",
        })
    return images


def _place_page(texts: list[dict], images: list[dict], start_heading: str) -> list[dict]:
    """按图的纵坐标插进本页正文，不重排正文。双栏不能整页按 y 排序。

    插在某行后面时，章节用那一行的；插在页首时用进入本页时已经生效的章节。
    没有坐标的图留在页尾，坐标保持空。
    """
    placed = [dict(block) for block in texts]
    located: list[dict] = []
    floating: list[dict] = []
    for image in images:
        copy = dict(image)
        if copy.get("bbox"):
            located.append(copy)
        else:
            floating.append(copy)
    located.sort(key=lambda image: (image["bbox"][1], image["bbox"][0]))
    for image in located:
        y = image["bbox"][1]
        insert_at = 0
        heading = start_heading
        for index, block in enumerate(placed):
            if block.get("block_kind") != "text":
                continue
            bbox = block.get("bbox")
            if not bbox or len(bbox) < 2:
                continue
            if bbox[1] < y:
                insert_at = index + 1
                heading = block.get("heading_path") or heading
        image["heading_path"] = heading
        placed.insert(insert_at, image)
    tail = start_heading
    for block in placed:
        if block.get("block_kind") == "text" and block.get("heading_path"):
            tail = block["heading_path"]
    for image in floating:
        image["heading_path"] = tail
        image["bbox"] = None
        placed.append(image)
    return placed


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
    texts_by_page: dict[int, list[dict]] = {}
    for number, page in enumerate(doc, start=1):
        png = page.get_pixmap(dpi=_RENDER_DPI)
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
        texts_by_page[number] = _blocks_from_layout_json(content, number)

    images_by_page: dict[int, list[dict]] = {}
    for number, page in enumerate(doc, start=1):
        images_by_page[number] = _page_images(doc, page, number)
    blocks: list[dict] = []
    for number in sorted(set(texts_by_page) | set(images_by_page)):
        # 版面解析每页自己起标题，页首没有从上一页带下来的章节。
        blocks.extend(_place_page(texts_by_page.get(number, []), images_by_page.get(number, []), ""))
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
            "bbox": _point_bbox(bbox),
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


def _point_bbox(value) -> list[float] | None:
    """版面模型的框是 _RENDER_DPI 下的像素，库存 PDF 点。"""
    box = _bbox(value)
    if not box:
        return None
    scale = 72 / _RENDER_DPI
    return [item * scale for item in box]


def _bbox(value) -> list[float] | None:
    if not value or len(value) < 4:
        return None
    return [float(value[0]), float(value[1]), float(value[2]), float(value[3])]


def _boxes_for_page(page, metas: list[tuple[int, int, int]]) -> list[list[float] | None]:
    """get_image_info 这版没有 xref。先用 get_image_rects，没有再按宽高认版面框。

    同一尺寸的多张图各拿一个坐标，从上到下分配。
    """
    infos = []
    try:
        infos = list(page.get_image_info() or [])
    except Exception:
        infos = []
    rects = []
    for xref, _width, _height in metas:
        rect = None
        try:
            found = page.get_image_rects(xref) or []
        except Exception:
            found = []
        if found:
            top = min(found, key=lambda item: (item.y0, item.x0))
            rect = [float(top.x0), float(top.y0), float(top.x1), float(top.y1)]
        rects.append(rect)
    return _match_info_boxes([(width, height) for _xref, width, height in metas], infos, rects)


def _match_info_boxes(metas, infos, rects) -> list[list[float] | None]:
    used: set[int] = set()
    boxes: list[list[float] | None] = []
    for (width, height), rect in zip(metas, rects):
        if rect:
            boxes.append(rect)
            continue
        chosen = None
        chosen_at = None
        chosen_index = None
        for index, info in enumerate(infos):
            if index in used or not info.get("bbox"):
                continue
            if width and height and (info.get("width") != width or info.get("height") != height):
                continue
            box = info["bbox"]
            key = (box[1], box[0])
            if chosen is None or key < chosen_at:
                chosen = box
                chosen_at = key
                chosen_index = index
        if chosen_index is None:
            boxes.append(None)
            continue
        used.add(chosen_index)
        boxes.append(_bbox(chosen))
    return boxes
