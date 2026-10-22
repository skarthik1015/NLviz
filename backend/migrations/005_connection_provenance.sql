-- ── Migration 005: where a connection's answers come from ───────────────
-- mode:      'live' (queried in place) or 'snapshot' (copied at copied_at)
-- notes:     why, in the user's words (shown in every session, not only at connect time)
-- Safe to run multiple times.

ALTER TABLE connections ADD COLUMN IF NOT EXISTS mode      TEXT        NOT NULL DEFAULT 'live';
ALTER TABLE connections ADD COLUMN IF NOT EXISTS copied_at TIMESTAMPTZ;
ALTER TABLE connections ADD COLUMN IF NOT EXISTS notes     JSONB       NOT NULL DEFAULT '[]';
