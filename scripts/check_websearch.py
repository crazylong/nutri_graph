"""探测智谱网页搜索的真实字段和耗时。字段名以这里打出来的为准。"""
from __future__ import annotations

import argparse
import time

import config
from kg.websearch import WebSearchError, search


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--query", required=True)
    args = parser.parse_args()
    if not config.WEB_SEARCH_API_KEY:
        print("WEB_SEARCH_API_KEY / ZHIPU_API_KEY 都是空的，没有发起搜索")
        return 1

    from zai import ZhipuAiClient

    client = ZhipuAiClient(api_key=config.WEB_SEARCH_API_KEY)
    started = time.perf_counter()
    try:
        response = client.web_search.web_search(
            search_engine="search_pro",
            search_query=args.query,
            count=config.WEB_SEARCH_TOP_K,
            search_recency_filter="noLimit",
        )
    except Exception as exc:
        print(f"raw_error {type(exc).__name__}: {exc}")
        return 1
    elapsed_ms = int((time.perf_counter() - started) * 1000)
    rows = getattr(response, "search_result", None) or []
    print(f"elapsed_ms={elapsed_ms} count={len(rows)}")
    if rows:
        first = rows[0]
        names = [name for name in dir(first) if not name.startswith("_")]
        print("fields:", ", ".join(names))
        if hasattr(first, "__dict__"):
            print("dict_keys:", ", ".join(vars(first)))
    try:
        mapped = search(args.query, client=client)
    except WebSearchError as exc:
        print(f"map_error {exc}")
        return 1
    for index, row in enumerate(mapped, 1):
        print(
            f"--- {index} title={row.title!r} site={row.site!r} "
            f"url={row.url!r} content_len={len(row.content)}"
        )
        print(row.content[:200])
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
