-- Index for efficient owner-scoped connection lookups
CREATE INDEX IF NOT EXISTS idx_connections_owner_id
    ON connections (owner_id)
    WHERE owner_id IS NOT NULL;
