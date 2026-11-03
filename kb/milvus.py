"""企业知识库的 Milvus 集合。显式 schema，稠密 + BM25，不碰 kb_chunks。"""
from __future__ import annotations

import logging
from typing import Any

from pymilvus import AnnSearchRequest, DataType, Function, FunctionType, WeightedRanker

import config
from kb.common import KbError
from rag.client import _get_client

log = logging.getLogger("kb.milvus")

_FIELDS = (
    "chunk_id",
    "doc_id",
    "tenant_id",
    "block_kind",
    "text",
    "heading_path",
    "image_path",
)


def ensure_collection() -> None:
    client = _get_client()
    name = config.KB_COLLECTION
    if client.has_collection(name):
        dim = _dense_dim(client, name)
        if dim != config.KB_EMBED_DIM:
            raise KbError(
                f"集合 {name} 的维度是 {dim}，当前模型是 {config.KB_EMBED_DIM}。换模型要换集合名。",
                status=503,
                code="collection_dim",
            )
        return
    schema = client.create_schema(auto_id=True, enable_dynamic_field=False)
    schema.add_field("id", DataType.INT64, is_primary=True)
    schema.add_field("chunk_id", DataType.VARCHAR, max_length=64)
    schema.add_field("doc_id", DataType.VARCHAR, max_length=64)
    schema.add_field("tenant_id", DataType.VARCHAR, max_length=32)
    schema.add_field("block_kind", DataType.VARCHAR, max_length=16)
    schema.add_field(
        "text",
        DataType.VARCHAR,
        max_length=4096,
        enable_analyzer=True,
        analyzer_params={"type": "chinese"},
    )
    schema.add_field("heading_path", DataType.VARCHAR, max_length=512)
    schema.add_field("image_path", DataType.VARCHAR, max_length=512)
    schema.add_field("dense", DataType.FLOAT_VECTOR, dim=config.KB_EMBED_DIM)
    schema.add_field("sparse", DataType.SPARSE_FLOAT_VECTOR)
    schema.add_function(Function(
        name="text_bm25",
        function_type=FunctionType.BM25,
        input_field_names=["text"],
        output_field_names=["sparse"],
    ))
    index = client.prepare_index_params()
    index.add_index(field_name="dense", index_type="AUTOINDEX", metric_type="COSINE")
    index.add_index(field_name="sparse", index_type="SPARSE_INVERTED_INDEX", metric_type="BM25")
    client.create_collection(collection_name=name, schema=schema, index_params=index)
    log.info("已建知识库集合 %s dim=%d", name, config.KB_EMBED_DIM)


def delete_doc(doc_id: str) -> None:
    client = _get_client()
    name = config.KB_COLLECTION
    if not client.has_collection(name):
        return
    client.delete(collection_name=name, filter=f'doc_id == "{doc_id}"')


def insert_chunks(rows: list[dict[str, Any]]) -> None:
    if not rows:
        return
    ensure_collection()
    client = _get_client()
    payload = []
    for row in rows:
        text = (row.get("text") or "").strip() or "图"
        payload.append({
            "chunk_id": row["chunk_id"],
            "doc_id": row["doc_id"],
            "tenant_id": row["tenant_id"],
            "block_kind": row["block_kind"],
            "text": _varchar(text, 4096),
            "heading_path": _varchar(row.get("heading_path") or "", 512),
            "image_path": _varchar(row.get("image_path") or "", 512),
            "dense": row["dense"],
        })
    client.insert(collection_name=config.KB_COLLECTION, data=payload)


def _varchar(text: str, limit: int) -> str:
    """字段上限按字节算。中文切 4000 个字仍可能超过 4096 字节。"""
    raw = text.encode("utf-8")
    if len(raw) <= limit:
        return text
    return raw[:limit].decode("utf-8", errors="ignore")


def search_text(tenant_id: str, query: str, dense: list[float], *, limit: int = 6) -> list[dict]:
    ensure_collection()
    client = _get_client()
    # 一张图两行。多取一截，调用方去重后再截回自己要的条数。
    fetch = _fetch(limit)
    expr = f'tenant_id == "{tenant_id}"'
    dense_req = AnnSearchRequest(
        data=[dense], anns_field="dense", param={"metric_type": "COSINE"}, limit=fetch, expr=expr,
    )
    sparse_req = AnnSearchRequest(
        data=[query], anns_field="sparse", param={"metric_type": "BM25"}, limit=fetch, expr=expr,
    )
    res = client.hybrid_search(
        collection_name=config.KB_COLLECTION,
        reqs=[dense_req, sparse_req],
        ranker=WeightedRanker(1.0, 1.0),
        limit=fetch,
        output_fields=list(_FIELDS),
    )
    return _hits(res)


def search_image(tenant_id: str, dense: list[float], *, limit: int = 6) -> list[dict]:
    ensure_collection()
    client = _get_client()
    res = client.search(
        collection_name=config.KB_COLLECTION,
        data=[dense],
        anns_field="dense",
        filter=f'tenant_id == "{tenant_id}"',
        limit=_fetch(limit),
        output_fields=list(_FIELDS),
    )
    return _hits(res)


def _fetch(limit: int) -> int:
    return max(int(limit) * 2, 20)


def _hits(res) -> list[dict]:
    out: list[dict] = []
    for hits in res or []:
        for hit in hits:
            ent = hit.get("entity") or {}
            out.append({
                "chunk_id": ent.get("chunk_id") or "",
                "doc_id": ent.get("doc_id") or "",
                "block_kind": ent.get("block_kind") or "",
                "text": ent.get("text") or "",
                "heading_path": ent.get("heading_path") or "",
                "image_path": ent.get("image_path") or "",
                "score": float(hit.get("distance") or 0.0),
            })
    return out


def _dense_dim(client, name: str) -> int:
    info = client.describe_collection(name)
    fields = info.get("fields") or []
    for field in fields:
        if field.get("name") != "dense":
            continue
        params = field.get("params") or field.get("type_params") or {}
        dim = params.get("dim")
        if dim:
            return int(dim)
    raise KbError(f"集合 {name} 没有 dense 字段", status=503, code="collection_dim")
