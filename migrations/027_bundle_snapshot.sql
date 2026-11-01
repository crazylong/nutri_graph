-- 027_bundle_snapshot.sql —— 补录来源快照

BEGIN;

ALTER TABLE kg_food_bundle
    ADD COLUMN IF NOT EXISTS snapshot_uri TEXT,
    ADD COLUMN IF NOT EXISTS snapshot_sha256 TEXT,
    ADD COLUMN IF NOT EXISTS fetched_at TIMESTAMPTZ;

COMMIT;
