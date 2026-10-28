-- 009_history.sql —— 历史会话列表
--
-- 不新建搜索词表：用户问过的话已经在 message 里。
-- 这里只补列表需要的标题，以及按用户列会话的索引。

BEGIN;

ALTER TABLE session ADD COLUMN IF NOT EXISTS title TEXT DEFAULT '';

CREATE INDEX IF NOT EXISTS idx_session_user ON session (user_id);

COMMIT;
