"""
rag/client.py —— Milvus 客户端封装

两个集合：
  kg_entity  —— 实体链接（节点 canonical_id + 别名文本的向量）
  kb_chunks  —— 文档块（降级检索用）

用 pymilvus 3.x 的 MilvusClient（而非旧的 connections.connect + Collection ORM API）：
  * 旧 ORM API 在 3.0 已标 deprecated、3.1 会移除，迁到 MilvusClient 一次到位
  * MilvusClient 自带连接管理，不再需要调用方先 connect() ——
    旧实现的 search_chunks 会报「should create connection first」就是漏了这步
"""
from __future__ import annotations

import logging
from typing import Any

from pymilvus import MilvusClient

import config

log = logging.getLogger("rag.client")

_client: MilvusClient | None = None


def _get_client() -> MilvusClient:
    global _client
    if _client is None:
        _client = MilvusClient(
            uri=config.MILVUS_URI,
            token=config.MILVUS_TOKEN or None,
        )
    return _client


def _ensure_collection(name: str, dim: int, desc: str) -> None:
    """集合不存在就建（含向量索引），存在则跳过。幂等。"""
    client = _get_client()
    if client.has_collection(name):
        return
    # MilvusClient 快速建集合：字段里带一个 float_vector，自动建主键 id
    client.create_collection(
        collection_name=name,
        dimension=dim,
        metric_type="COSINE",
        auto_id=True,
        description=desc,
    )
    # pymilvus 3.x 的 create_index 要 IndexParams 对象（不是 dict），
    # 用 prepare_index_params 构造；AUTOINDEX 免调参，够本项目规模。
    index_params = client.prepare_index_params(
        field_name="vector",
        index_type="AUTOINDEX",
        metric_type="COSINE",
    )
    client.create_index(collection_name=name, index_params=index_params)


# ============================================================
# 写入（实体索引 / 文档块）
# ============================================================
def insert_entity(
    canonical_id: str,
    label: str,
    name: str,
    text: str,
    embedding: list[float],
) -> None:
    """把实体写进 kg_entity（向量兜底用）。provisional 节点不应调用 —— 见 kg/promote.py。"""
    _ensure_collection(config.MILVUS_ENTITY_COLLECTION, config.EMBEDDING_DIM, "KG 实体链接")
    client = _get_client()
    client.insert(
        collection_name=config.MILVUS_ENTITY_COLLECTION,
        data=[{
            "canonical_id": canonical_id,
            "label": label,
            "name": name,
            "text": text,
            "vector": embedding,
        }],
    )


def delete_entity(canonical_id: str) -> None:
    """按 canonical_id 删掉 kg_entity 里的向量。集合还没有就什么都不做。"""
    if not canonical_id or '"' in canonical_id:
        return
    client = _get_client()
    name = config.MILVUS_ENTITY_COLLECTION
    if not client.has_collection(name):
        return
    client.delete(collection_name=name, filter=f'canonical_id == "{canonical_id}"')


def insert_chunk(doc_id: str, text: str, embedding: list[float]) -> None:
    _ensure_collection(config.MILVUS_CHUNK_COLLECTION, config.EMBEDDING_DIM, "文档块降级检索")
    client = _get_client()
    client.insert(
        collection_name=config.MILVUS_CHUNK_COLLECTION,
        data=[{"doc_id": doc_id, "text": text, "vector": embedding}],
    )


# ============================================================
# 检索
# ============================================================
def search_entity(embedding: list[float], top_k: int = 3) -> list[dict[str, Any]]:
    """实体链接向量检索，返回 [{canonical_id, label, name, score}]"""
    _ensure_collection(config.MILVUS_ENTITY_COLLECTION, config.EMBEDDING_DIM, "KG 实体链接")
    client = _get_client()
    res = client.search(
        collection_name=config.MILVUS_ENTITY_COLLECTION,
        data=[embedding],
        limit=top_k,
        output_fields=["canonical_id", "label", "name"],
    )
    out: list[dict[str, Any]] = []
    for hits in res:
        for h in hits:
            ent = h.get("entity", {})
            out.append(
                {
                    "canonical_id": ent.get("canonical_id"),
                    "label": ent.get("label"),
                    "name": ent.get("name"),
                    "score": float(h.get("distance", 0.0)),
                }
            )
    return out


def search_chunks(embedding: list[float], top_k: int = 6) -> list[dict[str, Any]]:
    """文档块向量检索，返回 [{doc_id, text, score}]"""
    _ensure_collection(config.MILVUS_CHUNK_COLLECTION, config.EMBEDDING_DIM, "文档块降级检索")
    client = _get_client()
    res = client.search(
        collection_name=config.MILVUS_CHUNK_COLLECTION,
        data=[embedding],
        limit=top_k,
        output_fields=["doc_id", "text"],
    )
    out: list[dict[str, Any]] = []
    for hits in res:
        for h in hits:
            ent = h.get("entity", {})
            out.append(
                {
                    "doc_id": ent.get("doc_id"),
                    "text": ent.get("text"),
                    "score": float(h.get("distance", 0.0)),
                }
            )
    return out
