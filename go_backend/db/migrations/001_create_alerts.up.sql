-- ── Alerts ───────────────────────────────────────────────────────────────────
CREATE TABLE IF NOT EXISTS alerts (
    alert_id         TEXT PRIMARY KEY DEFAULT gen_random_uuid()::text,
    source           TEXT NOT NULL CHECK (source IN ('prometheus','datadog','grafana','custom_webhook')),
    name             TEXT NOT NULL,
    severity         TEXT NOT NULL CHECK (severity IN ('critical','high','medium','low','info')),
    status           TEXT NOT NULL DEFAULT 'firing' CHECK (status IN ('firing','resolved','suppressed')),
    fired_at         TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    resolved_at      TIMESTAMPTZ,
    labels           JSONB NOT NULL DEFAULT '{}',
    annotations      JSONB NOT NULL DEFAULT '{}',
    affected_services TEXT[] NOT NULL DEFAULT '{}',
    generator_url    TEXT,
    created_at       TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at       TIMESTAMPTZ NOT NULL DEFAULT NOW()
);
CREATE INDEX IF NOT EXISTS idx_alerts_status   ON alerts(status);
CREATE INDEX IF NOT EXISTS idx_alerts_severity ON alerts(severity);
CREATE INDEX IF NOT EXISTS idx_alerts_fired_at ON alerts(fired_at DESC);
