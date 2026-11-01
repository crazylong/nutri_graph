-- 029_kb.sql —— 企业知识库文档、块、离线评估结果
-- 向量在 Milvus 新集合里，这里只存主数据和派生块。

BEGIN;

CREATE TABLE IF NOT EXISTS kb_document (
    id                  UUID PRIMARY KEY,
    tenant_id           TEXT NOT NULL,
    title               TEXT NOT NULL,
    doc_kind            TEXT NOT NULL,
    status              TEXT NOT NULL,
    filename            TEXT NOT NULL,
    storage_path        TEXT NOT NULL,
    uploader_user_id    TEXT NOT NULL,
    uploader_phone_mask TEXT NOT NULL,
    error               TEXT NOT NULL DEFAULT '',
    created_at          TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at          TIMESTAMPTZ NOT NULL DEFAULT now(),
    CONSTRAINT kb_document_kind CHECK (doc_kind IN ('tech', 'policy')),
    CONSTRAINT kb_document_status CHECK (status IN ('pending', 'approved', 'rejected'))
);

CREATE INDEX IF NOT EXISTS idx_kb_document_tenant ON kb_document (tenant_id);
CREATE INDEX IF NOT EXISTS idx_kb_document_status ON kb_document (status, updated_at DESC);

CREATE TABLE IF NOT EXISTS kb_chunk (
    id           TEXT PRIMARY KEY,
    doc_id       UUID NOT NULL REFERENCES kb_document(id) ON DELETE CASCADE,
    tenant_id    TEXT NOT NULL,
    block_kind   TEXT NOT NULL,
    text         TEXT NOT NULL,
    page_no      INT,
    bbox         JSONB,
    heading_path TEXT NOT NULL DEFAULT '',
    image_path   TEXT,
    image_md5    TEXT,
    CONSTRAINT kb_chunk_kind CHECK (block_kind IN ('text', 'image'))
);

CREATE INDEX IF NOT EXISTS idx_kb_chunk_doc ON kb_chunk (doc_id);

CREATE TABLE IF NOT EXISTS kb_eval_run (
    id         UUID PRIMARY KEY,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    ragas      JSONB NOT NULL,
    recall     JSONB NOT NULL
);

COMMIT;
