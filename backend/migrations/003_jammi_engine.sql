-- ── Migration 003: Jammi engine columns (Phase 1d) ──────────────────────
-- engine:       'legacy' (v1 direct connectors) or 'jammi' (data registered as Jammi sources)
-- workspace_id: isolation unit (decision A1); the owner's personal workspace for now
-- jammi_tables: logical table name -> "<source>.public.<table>" for engine = 'jammi'
-- Safe to run multiple times.

ALTER TABLE connections ADD COLUMN IF NOT EXISTS engine       TEXT  NOT NULL DEFAULT 'legacy';
ALTER TABLE connections ADD COLUMN IF NOT EXISTS workspace_id TEXT;
ALTER TABLE connections ADD COLUMN IF NOT EXISTS jammi_tables JSONB NOT NULL DEFAULT '{}';

CREATE INDEX IF NOT EXISTS idx_connections_workspace_id
    ON connections (workspace_id)
    WHERE workspace_id IS NOT NULL;
