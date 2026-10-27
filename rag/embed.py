"""
rag/embed.py —— DashScope Embedding 封装

使用 langchain-openai 兼容协议指向 DashScope 的 OpenAI 兼容端点。
"""
from __future__ import annotations

from langchain_openai import OpenAIEmbeddings

import config


def get_embeddings() -> OpenAIEmbeddings:
    """返回 DashScope embedding 客户端（单例可按需缓存）。

    `check_embedding_ctx_length=False` 是必须的，不是可选优化：

    langchain-openai 0.3.x 默认开了这个开关，会先用 tiktoken 把文本切成 token id
    再发 `input=[[...]]`（OpenAI 官方接受这种形式）。DashScope 的 OpenAI 兼容端点
    不接受，报一个极具误导性的错：

        400 InternalError.Algo.InvalidParameter:
        Value error, contents is neither str nor list of str.: input.contents

    直接在 httpx 里发 `input=["黑豆"]` 是 200，所以问题不在 key、不在模型名。
    关掉之后 SDK 原样发字符串数组，正常返回 1024 维。
    """
    return OpenAIEmbeddings(
        model=config.EMBEDDING_MODEL,
        api_key=config.DASHSCOPE_API_KEY,
        base_url=config.DASHSCOPE_BASE_URL,
        dimensions=config.EMBEDDING_DIM,
        check_embedding_ctx_length=False,
    )
