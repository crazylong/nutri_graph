"""一次性核对：中文问句的独立文本向量能不能召回架构图的独立图像向量。

用法（在服务器上，且已配置 DASHSCOPE_API_KEY）：

    python -m kb._smoke_embed --image a.png --question "这张架构图讲了什么"
"""
from __future__ import annotations

import argparse

from kb.embed_mm import embed_image_bytes, embed_text


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--image", action="append", required=True)
    parser.add_argument("--question", action="append", required=True)
    args = parser.parse_args()
    images = []
    for path in args.image:
        raw = open(path, "rb").read()
        images.append((path, embed_image_bytes(raw, mime=_mime(path))))
    for question in args.question:
        query = embed_text(question)
        ranked = sorted(
            ((name, _cosine(query, vec)) for name, vec in images),
            key=lambda item: item[1],
            reverse=True,
        )
        print(question)
        for index, (name, score) in enumerate(ranked, start=1):
            print(f"  {index}. {score:.4f}  {name}")
        top = [name for name, _ in ranked[:3]]
        print("前 3：", ", ".join(top))


def _cosine(a: list[float], b: list[float]) -> float:
    dot = sum(x * y for x, y in zip(a, b))
    na = sum(x * x for x in a) ** 0.5
    nb = sum(y * y for y in b) ** 0.5
    if na == 0 or nb == 0:
        return 0.0
    return dot / (na * nb)


def _mime(path: str) -> str:
    lower = path.lower()
    if lower.endswith(".jpg") or lower.endswith(".jpeg"):
        return "image/jpeg"
    if lower.endswith(".webp"):
        return "image/webp"
    return "image/png"


if __name__ == "__main__":
    main()
