-- 020_turns.sql —— 轮次标识 + 滚动摘要水位线
--
-- 为什么必须要有 turn_no：原来靠 created_at + 角色配对切轮，
-- 遇到「只落了 user、没落 assistant」的轮次（红旗短路那一轮）就会切错，
-- 而且窗口按"条"取会在轮中间切开。

ALTER TABLE message ADD COLUMN IF NOT EXISTS turn_no INT DEFAULT 0;
-- kind 先留位置（human / tool / ai）。v3 不写 tool 消息 ——
-- 图里现在只有 human/ai，别为一个不存在的场景写逻辑。
ALTER TABLE message ADD COLUMN IF NOT EXISTS kind TEXT DEFAULT '';

-- 水位线：id <= summary_until_msg_id 的消息已经被摘要覆盖
ALTER TABLE session ADD COLUMN IF NOT EXISTS summary_until_msg_id BIGINT DEFAULT 0;
ALTER TABLE session ADD COLUMN IF NOT EXISTS summary_updated_at TIMESTAMPTZ;

CREATE INDEX IF NOT EXISTS idx_message_session_turn ON message (session_id, turn_no);

-- 存量补 turn_no：按 (session_id, created_at, id) 排序后两两一轮。
-- 只在 turn_no 还是默认的 0 时补，重跑不会覆盖手工修正过的值。
--
-- 注意：这个补法对「历史里缺 assistant 的轮次」是不准的（红旗短路那些轮），
-- 但存量数据本来就没有可靠的轮边界可用 —— 它比 turn_no 全 0 好，
-- 且不影响之后新写入的轮次。
WITH ordered AS (
    SELECT id,
           session_id,
           ROW_NUMBER() OVER (PARTITION BY session_id ORDER BY created_at ASC, id ASC) AS rn
    FROM message
    WHERE turn_no = 0
)
UPDATE message m
SET turn_no = ((o.rn - 1) / 2) + 1
FROM ordered o
WHERE m.id = o.id;
