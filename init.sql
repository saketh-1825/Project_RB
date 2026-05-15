-- ─────────────────────────────────────────────────────────────────────────────
--  SRE Copilot — PostgreSQL init
--  Runs once on first container start.
-- ─────────────────────────────────────────────────────────────────────────────

CREATE EXTENSION IF NOT EXISTS "uuid-ossp";
CREATE EXTENSION IF NOT EXISTS vector;

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

-- ── Incidents ────────────────────────────────────────────────────────────────
CREATE TABLE IF NOT EXISTS incidents (
    incident_id       TEXT PRIMARY KEY DEFAULT gen_random_uuid()::text,
    alert_id          TEXT NOT NULL REFERENCES alerts(alert_id),
    title             TEXT NOT NULL,
    severity          TEXT NOT NULL CHECK (severity IN ('critical','high','medium','low','info')),
    status            TEXT NOT NULL DEFAULT 'open' CHECK (status IN ('open','resolved')),
    affected_services TEXT[] NOT NULL DEFAULT '{}',
    opened_by         TEXT NOT NULL,
    opened_at         TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    resolved_at       TIMESTAMPTZ,
    updated_at        TIMESTAMPTZ NOT NULL DEFAULT NOW()
);
CREATE INDEX IF NOT EXISTS idx_incidents_status    ON incidents(status);
CREATE INDEX IF NOT EXISTS idx_incidents_alert_id  ON incidents(alert_id);
CREATE INDEX IF NOT EXISTS idx_incidents_opened_at ON incidents(opened_at DESC);

-- ── Incident Events (findings streamed during analysis) ───────────────────────
CREATE TABLE IF NOT EXISTS incident_events (
    finding_id   TEXT PRIMARY KEY DEFAULT gen_random_uuid()::text,
    incident_id  TEXT NOT NULL REFERENCES incidents(incident_id),
    agent        TEXT NOT NULL,
    type         TEXT NOT NULL,
    severity     TEXT NOT NULL,
    title        TEXT NOT NULL,
    summary      TEXT NOT NULL,
    evidence     JSONB NOT NULL DEFAULT '{}',
    confidence   NUMERIC(4,3) CHECK (confidence >= 0 AND confidence <= 1),
    created_at   TIMESTAMPTZ NOT NULL DEFAULT NOW()
);
CREATE INDEX IF NOT EXISTS idx_incident_events_incident_id ON incident_events(incident_id);

-- ── Incident Reports ─────────────────────────────────────────────────────────
CREATE TABLE IF NOT EXISTS incident_reports (
    report_id           TEXT PRIMARY KEY DEFAULT gen_random_uuid()::text,
    incident_id         TEXT NOT NULL REFERENCES incidents(incident_id),
    alert_id            TEXT NOT NULL,
    generated_at        TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    title               TEXT NOT NULL,
    executive_summary   TEXT NOT NULL,
    root_cause          JSONB NOT NULL DEFAULT '{}',
    timeline            JSONB NOT NULL DEFAULT '[]',
    suggested_fixes     JSONB NOT NULL DEFAULT '[]',
    similar_past_incidents JSONB NOT NULL DEFAULT '[]',
    runbooks_consulted  JSONB NOT NULL DEFAULT '[]',
    model_metadata      JSONB NOT NULL DEFAULT '{}'
);

-- ── Runbooks (text + vector embedding) ───────────────────────────────────────
CREATE TABLE IF NOT EXISTS runbooks (
    runbook_id   TEXT PRIMARY KEY DEFAULT gen_random_uuid()::text,
    title        TEXT NOT NULL,
    tags         TEXT[] NOT NULL DEFAULT '{}',
    services     TEXT[] NOT NULL DEFAULT '{}',
    content      TEXT NOT NULL,
    embedding    vector(1536),          -- OpenAI/Anthropic embedding dimension
    last_updated TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    created_at   TIMESTAMPTZ NOT NULL DEFAULT NOW()
);
CREATE INDEX IF NOT EXISTS idx_runbooks_services ON runbooks USING GIN(services);
CREATE INDEX IF NOT EXISTS idx_runbooks_tags     ON runbooks USING GIN(tags);
-- IVFFlat index for fast approximate nearest-neighbour search
-- Create AFTER seeding data: CREATE INDEX ON runbooks USING ivfflat (embedding vector_cosine_ops) WITH (lists = 100);

-- ── Analyses (LangGraph analysis sessions) ───────────────────────────────────
CREATE TABLE IF NOT EXISTS analyses (
    analysis_id            TEXT PRIMARY KEY DEFAULT gen_random_uuid()::text,
    alert_id               TEXT NOT NULL,
    incident_id            TEXT REFERENCES incidents(incident_id),
    status                 TEXT NOT NULL DEFAULT 'pending'
                               CHECK (status IN ('pending','running','awaiting_human','completed','failed','cancelled')),
    current_agent          TEXT,
    steps_completed        INTEGER NOT NULL DEFAULT 0,
    steps_total            INTEGER NOT NULL DEFAULT 6,
    current_step_desc      TEXT,
    findings_count         INTEGER NOT NULL DEFAULT 0,
    report_id              TEXT,
    error_message          TEXT,
    started_at             TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    completed_at           TIMESTAMPTZ,
    updated_at             TIMESTAMPTZ NOT NULL DEFAULT NOW()
);
CREATE INDEX IF NOT EXISTS idx_analyses_alert_id  ON analyses(alert_id);
CREATE INDEX IF NOT EXISTS idx_analyses_status    ON analyses(status);
CREATE INDEX IF NOT EXISTS idx_analyses_started_at ON analyses(started_at DESC);

-- ── Seed: sample runbook so RAG has something to find ────────────────────────
INSERT INTO runbooks (runbook_id, title, tags, services, content, last_updated)
VALUES (
    'rb-001',
    'PostgreSQL Connection Pool Exhaustion',
    ARRAY['database','postgresql','connection-pool','payment-api'],
    ARRAY['payment-api','order-service'],
    E'## Symptoms\n- `db_pool_waiting_connections` metric spikes above 10\n- Service returns 503 errors\n- Logs show: `connection pool exhausted`\n\n## Root Cause\nMost commonly caused by:\n1. A slow query holding connections for too long\n2. A database primary failover\n3. A recent deployment that increased connection demand\n\n## Resolution Steps\n1. Check `pg_stat_activity` for long-running queries\n2. Kill offending queries if safe\n3. Increase `max_connections` if recurring\n4. Scale service replicas\n5. Roll back last deployment if it coincided with the issue',
    NOW()
) ON CONFLICT DO NOTHING;

INSERT INTO runbooks (runbook_id, title, tags, services, content, last_updated)
VALUES (
    'rb-002',
    'Cascading Failure: Upstream Service Timeout',
    ARRAY['cascading-failure','timeout','circuit-breaker'],
    ARRAY['api-gateway','order-service','payment-api'],
    E'## Symptoms\n- Multiple services showing elevated error rates simultaneously\n- Traces show timeouts propagating from a single downstream service\n\n## Resolution Steps\n1. Identify the root service using distributed traces\n2. Enable circuit breaker on the failing downstream service\n3. Shed load at the edge if needed',
    NOW()
) ON CONFLICT DO NOTHING;