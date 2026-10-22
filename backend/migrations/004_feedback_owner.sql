-- ── Migration 004: feedback belongs to a user (review B15) ──────────────
-- Feedback was unauthenticated and its idempotency key global: any caller could overwrite another
-- user's record by reusing its key. Keys are now unique per owner. Safe to run multiple times.

ALTER TABLE feedback ADD COLUMN IF NOT EXISTS owner_id TEXT;
ALTER TABLE feedback DROP CONSTRAINT IF EXISTS feedback_idempotency_key_key;
CREATE UNIQUE INDEX IF NOT EXISTS idx_feedback_owner_idempotency
    ON feedback (owner_id, idempotency_key);
