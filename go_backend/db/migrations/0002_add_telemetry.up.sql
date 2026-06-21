-- ─────────────────────────────────────────────────────────────────────────────
--  0002: Add telemetry tables (logs, metrics, traces, services)
--  Required by the contract for GET /logs, GET /metrics/*, GET /traces/*, GET /services/*
-- ─────────────────────────────────────────────────────────────────────────────

-- ── Logs ─────────────────────────────────────────────────────────────────────
CREATE TABLE IF NOT EXISTS logs (
    log_id      TEXT PRIMARY KEY DEFAULT gen_random_uuid()::text,
    timestamp   TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    level       TEXT NOT NULL CHECK (level IN ('DEBUG','INFO','WARN','ERROR','FATAL')),
    service     TEXT NOT NULL,
    host        TEXT NOT NULL DEFAULT '',
    message     TEXT NOT NULL,
    trace_id    TEXT,
    span_id     TEXT,
    attributes  JSONB NOT NULL DEFAULT '{}',
    created_at  TIMESTAMPTZ NOT NULL DEFAULT NOW()
);
CREATE INDEX IF NOT EXISTS idx_logs_timestamp ON logs(timestamp DESC);
CREATE INDEX IF NOT EXISTS idx_logs_service   ON logs(service);
CREATE INDEX IF NOT EXISTS idx_logs_level     ON logs(level);
CREATE INDEX IF NOT EXISTS idx_logs_trace_id  ON logs(trace_id) WHERE trace_id IS NOT NULL;
CREATE INDEX IF NOT EXISTS idx_logs_message_fts ON logs USING GIN(to_tsvector('english', message));

-- ── Metric Catalog ──────────────────────────────────────────────────────────
CREATE TABLE IF NOT EXISTS metric_catalog (
    metric_name TEXT PRIMARY KEY,
    description TEXT NOT NULL DEFAULT '',
    labels      TEXT[] NOT NULL DEFAULT '{}',
    unit        TEXT NOT NULL DEFAULT ''
);

-- ── Metric Data Points ──────────────────────────────────────────────────────
CREATE TABLE IF NOT EXISTS metric_data (
    id          BIGSERIAL PRIMARY KEY,
    metric_name TEXT NOT NULL,
    timestamp   TIMESTAMPTZ NOT NULL,
    value       DOUBLE PRECISION NOT NULL,
    labels      JSONB NOT NULL DEFAULT '{}'
);
CREATE INDEX IF NOT EXISTS idx_metric_data_name_ts ON metric_data(metric_name, timestamp DESC);

-- ── Spans (distributed traces) ──────────────────────────────────────────────
CREATE TABLE IF NOT EXISTS spans (
    span_id        TEXT PRIMARY KEY,
    trace_id       TEXT NOT NULL,
    parent_span_id TEXT,
    service        TEXT NOT NULL,
    operation      TEXT NOT NULL,
    start_time     TIMESTAMPTZ NOT NULL,
    duration_ms    DOUBLE PRECISION NOT NULL,
    status         TEXT NOT NULL CHECK (status IN ('ok','error','timeout')),
    attributes     JSONB NOT NULL DEFAULT '{}',
    error_message  TEXT
);
CREATE INDEX IF NOT EXISTS idx_spans_trace_id   ON spans(trace_id);
CREATE INDEX IF NOT EXISTS idx_spans_service    ON spans(service);
CREATE INDEX IF NOT EXISTS idx_spans_start_time ON spans(start_time DESC);
CREATE INDEX IF NOT EXISTS idx_spans_status     ON spans(status);

-- ── Services ────────────────────────────────────────────────────────────────
CREATE TABLE IF NOT EXISTS services (
    service_id        TEXT PRIMARY KEY DEFAULT gen_random_uuid()::text,
    name              TEXT NOT NULL UNIQUE,
    health            TEXT NOT NULL DEFAULT 'unknown'
                          CHECK (health IN ('healthy','degraded','down','unknown')),
    version           TEXT NOT NULL DEFAULT '',
    tags              JSONB NOT NULL DEFAULT '{}',
    error_rate_1m     DOUBLE PRECISION NOT NULL DEFAULT 0,
    p99_latency_ms    DOUBLE PRECISION NOT NULL DEFAULT 0,
    active_instances  INTEGER NOT NULL DEFAULT 0,
    last_deploy_at    TIMESTAMPTZ,
    last_deploy_version TEXT,
    last_deploy_by    TEXT,
    updated_at        TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

-- ── Service Dependencies ────────────────────────────────────────────────────
CREATE TABLE IF NOT EXISTS service_dependencies (
    id              BIGSERIAL PRIMARY KEY,
    service_id      TEXT NOT NULL REFERENCES services(service_id),
    depends_on_id   TEXT NOT NULL REFERENCES services(service_id),
    call_type       TEXT NOT NULL CHECK (call_type IN ('sync','async','db','cache','queue')),
    avg_latency_ms     DOUBLE PRECISION NOT NULL DEFAULT 0,
    error_rate_percent DOUBLE PRECISION NOT NULL DEFAULT 0
);
CREATE INDEX IF NOT EXISTS idx_svc_deps_service ON service_dependencies(service_id);
