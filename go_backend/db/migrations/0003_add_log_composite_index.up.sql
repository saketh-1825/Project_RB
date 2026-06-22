-- ─────────────────────────────────────────────────────────────────────────────
--  0003: Add composite index on logs(service, timestamp, level)
--  Optimises the most common query pattern: filtering by service in a time
--  range with optional level filtering.
-- ─────────────────────────────────────────────────────────────────────────────

CREATE INDEX IF NOT EXISTS idx_logs_service_ts_level
    ON logs (service, timestamp DESC, level);
