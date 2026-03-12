-- ── Migration 001: Initial schema ────────────────────────────────────
-- Creates the three core tables and supporting indexes.
-- Safe to run multiple times (all objects use IF NOT EXISTS).

-- ── Connections ───────────────────────────────────────────────────────

CREATE TABLE IF NOT EXISTS connections (
    connection_id   TEXT        PRIMARY KEY,
    display_name    TEXT        NOT NULL,
    connector_type  TEXT        NOT NULL,
    status          TEXT        NOT NULL DEFAULT 'active',
    owner_id        TEXT,
    denied_columns  JSONB       NOT NULL DEFAULT '[]',
    created_at      TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at      TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_connections_status
    ON connections (status);

-- ── Schema versions ───────────────────────────────────────────────────

CREATE TABLE IF NOT EXISTS schema_versions (
    version_id          TEXT        PRIMARY KEY,
    connection_id       TEXT        NOT NULL REFERENCES connections(connection_id),
    status              TEXT        NOT NULL DEFAULT 'draft',
    schema_path         TEXT        NOT NULL,
    validation_summary  JSONB,
    generation_metadata JSONB,
    created_at          TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at          TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_schema_versions_connection
    ON schema_versions (connection_id, status);

-- ── Feedback ──────────────────────────────────────────────────────────

CREATE TABLE IF NOT EXISTS feedback (
    feedback_id     TEXT        PRIMARY KEY,
    query_id        TEXT        NOT NULL,
    rating          TEXT        NOT NULL,
    comment         TEXT,
    idempotency_key TEXT        UNIQUE,
    created_at      TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_feedback_query_id
    ON feedback (query_id);
