-- 032_kb_doc_kind_qa.sql —— 知识库分类增加「问答类」（qa）
-- 029 里的 kb_document_kind 只允许 tech / policy，这里放开到三个值。
-- 存量数据不动：既有文档的分类仍是 tech 或 policy。

BEGIN;

ALTER TABLE kb_document DROP CONSTRAINT IF EXISTS kb_document_kind;
ALTER TABLE kb_document
    ADD CONSTRAINT kb_document_kind CHECK (doc_kind IN ('tech', 'policy', 'qa'));

COMMIT;
