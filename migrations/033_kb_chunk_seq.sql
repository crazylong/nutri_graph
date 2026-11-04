-- 033_kb_chunk_seq.sql —— 知识库块的阅读顺序
-- 相邻块按这个序号取前一块和后一块。已有行保持空，不在这里回填。

BEGIN;

ALTER TABLE kb_chunk ADD COLUMN IF NOT EXISTS seq INT;

CREATE UNIQUE INDEX IF NOT EXISTS idx_kb_chunk_doc_seq ON kb_chunk (doc_id, seq);

COMMIT;
