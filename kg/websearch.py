"""
kg/websearch.py —— 智谱网页搜索适配

普通函数，不用 @tool。参考实现只把 content 拼成字符串，标题和链接被丢掉，
这里必须单独取出来。字段名以探测脚本打出来的为准；下面的名字是
Zhipu search_result 上已经见到的属性，取不到就留空，不编一个。
"""
from __future__ import annotations

import logging
from concurrent.futures import ThreadPoolExecutor, TimeoutError as FutTimeout
from dataclasses import dataclass
from urllib.parse import urlparse

import config

log = logging.getLogger("kg.websearch")


class WebSearchError(Exception):
    """搜索没发出去，或对方没有按预期返回。调用方回落到未收录话术。"""


@dataclass
class WebResult:
    title: str
    content: str
    url: str
    site: str


def _text(obj, *names: str) -> str:
    if isinstance(obj, dict):
        for name in names:
            value = obj.get(name)
            if value:
                return str(value).strip()
        return ""
    for name in names:
        value = getattr(obj, name, None)
        if value:
            return str(value).strip()
    return ""


def _host(url: str) -> str:
    if not url:
        return ""
    host = urlparse(url).hostname or ""
    if host.startswith("www."):
        host = host[4:]
    return host


def _map_row(row) -> WebResult:
    url = _text(row, "link", "url")
    site = _host(url) or _text(row, "media", "site")
    return WebResult(
        title=_text(row, "title"),
        content=_text(row, "content"),
        url=url,
        site=site,
    )


def _call(client, query: str, top_k: int):
    return client.web_search.web_search(
        search_engine="search_pro",
        search_query=query,
        count=top_k,
        search_recency_filter="noLimit",
    )


def search(
    query: str,
    *,
    top_k: int | None = None,
    timeout_s: float | None = None,
    client=None,
) -> list[WebResult]:
    """搜一回。client 可注入：测试传一个带 web_search.web_search 的假对象。

    provider=null 时直接空列表，不碰网络。没配 key 时抛 WebSearchError('no_key')。
    """
    if config.WEB_SEARCH_PROVIDER == "null":
        return []
    top_k = config.WEB_SEARCH_TOP_K if top_k is None else top_k
    timeout_s = config.WEB_SEARCH_TIMEOUT_S if timeout_s is None else timeout_s
    owns_client = client is None
    if owns_client:
        if not config.WEB_SEARCH_API_KEY:
            raise WebSearchError("no_key")
        from zai import ZhipuAiClient

        client = ZhipuAiClient(api_key=config.WEB_SEARCH_API_KEY)

    pool = ThreadPoolExecutor(max_workers=1, thread_name_prefix="web-search")
    fut = pool.submit(_call, client, query, top_k)
    try:
        response = fut.result(timeout=timeout_s)
    except FutTimeout as exc:
        raise WebSearchError("timeout") from exc
    except WebSearchError:
        raise
    except Exception as exc:
        raise WebSearchError(f"{type(exc).__name__}: {exc}") from exc
    finally:
        pool.shutdown(wait=False, cancel_futures=True)

    rows = getattr(response, "search_result", None) or []
    return [_map_row(row) for row in rows]
