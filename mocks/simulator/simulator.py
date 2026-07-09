#!/usr/bin/env python3
"""
Telemetry Simulator — generates realistic structured logs AND metrics, POSTs
them to the Go backend's /internal/{logs,metrics}/ingest endpoints.

Spike mode (every SPIKE_INTERVAL_SECONDS) injects correlated errors and
metric anomalies across BOTH payment-api AND order-service simultaneously,
matching the Week 3 contract integration test expectations.

Environment variables:
    GO_BACKEND_URL          — e.g. http://go-backend:8080
    SRE_INTERNAL_TOKEN      — Bearer token for auth
    SERVICES                — comma-separated service names
    LOG_RATE_PER_SECOND     — target log lines per second       (default 20)
    METRICS_RATE_PER_SECOND — metric points per second          (default 10)
    ERROR_RATE              — baseline error fraction           (default 0.04)
    SPIKE_INTERVAL_SECONDS  — inject error spike every N s      (default 120)
    SPIKE_DURATION_SECONDS  — how long each spike lasts         (default 10)
"""

import json
import logging
import os
import random
import signal
import sys
import threading
import time
import uuid
from datetime import datetime, timezone

import requests

# ── Configuration ─────────────────────────────────────────────────────────────

BACKEND_URL     = os.getenv("GO_BACKEND_URL", "http://go-backend:8080")
TOKEN           = os.getenv("SRE_INTERNAL_TOKEN", "")
SERVICES        = os.getenv(
    "SERVICES",
    "payment-api,auth-service,order-service,notification-service,api-gateway",
).split(",")
LOG_RATE        = int(os.getenv("LOG_RATE_PER_SECOND", "20"))
METRICS_RATE    = int(os.getenv("METRICS_RATE_PER_SECOND", "10"))
ERROR_RATE      = float(os.getenv("ERROR_RATE", "0.04"))
SPIKE_INTERVAL  = int(os.getenv("SPIKE_INTERVAL_SECONDS", "120"))
SPIKE_DURATION  = int(os.getenv("SPIKE_DURATION_SECONDS", "10"))

# Services that get hit during a correlated spike
SPIKE_SERVICES  = ["payment-api", "order-service"]

LOG_BATCH_SIZE  = max(LOG_RATE, 20)
METRIC_BATCH    = max(METRICS_RATE, 10)

# ── Log templates ─────────────────────────────────────────────────────────────

LEVELS               = ["DEBUG", "INFO", "INFO", "INFO", "WARN", "ERROR", "FATAL"]
LEVEL_WEIGHTS_NORMAL = [5, 50, 50, 50, 10, 4, 1]
LEVEL_WEIGHTS_SPIKE  = [1, 10, 10, 10, 15, 40, 14]

HOSTS = [
    "prod-us-east-1a-{svc}-01",
    "prod-us-east-1b-{svc}-02",
    "prod-us-west-2a-{svc}-01",
]

LOG_MESSAGES = {
    "DEBUG": [
        "Cache miss for key user:{uid}",
        "Retrying gRPC call to {dep}, attempt 2/3",
        "Connection pool stats: active=12 idle=8 waiting=0",
        "JWT token validated for subject {uid}",
        "Evaluating feature flag rollout-{svc} => enabled",
    ],
    "INFO": [
        "Request completed: {method} {path} → 200 in {latency}ms",
        "Processed order {oid} for user {uid}, total=${amount}",
        "Health check passed: db=ok redis=ok upstream=ok",
        "Deployed version v2.{minor}.{patch} successfully",
        "Rate limiter: 142/500 requests in current window",
        "Background job batch_reconcile completed in {latency}ms",
        "WebSocket connection established for session {sid}",
        "Metric exported: {metric}={value}",
    ],
    "WARN": [
        "Slow query detected: SELECT … FROM orders WHERE … took {latency}ms",
        "Connection pool nearing capacity: 18/20 active connections",
        "Upstream {dep} responded with 429 Too Many Requests",
        "Certificate for {dep}.internal expires in 14 days",
        "Memory usage at 82% of limit (820Mi / 1Gi)",
        "Request timeout approaching: {latency}ms of 3000ms budget used",
    ],
    "ERROR": [
        "Failed to process payment for order {oid}: gateway timeout after {latency}ms",
        "Database connection failed: dial tcp {dep}:5432: connection refused",
        "Unhandled exception in handler {path}: NullPointerError",
        "Circuit breaker OPEN for {dep}: 5 consecutive failures",
        "Message publish to queue orders.created failed: channel closed",
        "TLS handshake failed with {dep}: certificate verify failed",
    ],
    "FATAL": [
        "Out of memory: container killed by OOM (used 1.2Gi / 1Gi limit)",
        "Database migration failed: duplicate column 'status' in table 'orders'",
        "Unable to bind to port 8080: address already in use",
    ],
}

METHODS      = ["GET", "POST", "PUT", "PATCH", "DELETE"]
PATHS        = [
    "/api/v1/orders", "/api/v1/orders/{oid}",
    "/api/v1/payments", "/api/v1/payments/{oid}/capture",
    "/api/v1/users/{uid}", "/api/v1/users/{uid}/profile",
    "/api/v1/auth/login", "/api/v1/auth/refresh",
    "/api/v1/notifications/send", "/api/v1/health",
]
METRICS_LIST = [
    "http_request_duration_seconds", "db_pool_active_connections",
    "order_processing_duration_ms", "cache_hit_ratio",
    "queue_depth", "error_rate_1m",
]
DEPENDENCIES = ["postgres", "redis", "kafka", "auth-service", "payment-gateway", "order-service"]

# ── Metric definitions per service ────────────────────────────────────────────

# Normal baseline ranges: (min, max)
METRIC_BASELINES = {
    "http_error_rate":                   (0.01, 0.05),   # 1-5%
    "http_request_duration_p99":         (50, 300),       # ms
    "db_pool_active_connections":        (2, 14),
    "queue_depth":                       (0, 50),
    "memory_usage_bytes":                (200e6, 700e6),
    "cpu_usage_percent":                 (10, 60),
    "cache_hit_ratio":                   (0.75, 0.98),
}

# Spike overrides: (min, max) — extreme values during spike
METRIC_SPIKES = {
    "http_error_rate":            (0.35, 0.75),
    "http_request_duration_p99":  (2000, 9000),
    "db_pool_active_connections": (18, 20),
    "queue_depth":                (500, 2000),
    "memory_usage_bytes":         (900e6, 1.1e9),
    "cpu_usage_percent":          (85, 100),
    "cache_hit_ratio":            (0.05, 0.25),
}

# ── Logging setup ─────────────────────────────────────────────────────────────

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("simulator")

# ── Graceful shutdown ─────────────────────────────────────────────────────────

running = True

def _shutdown(signum, frame):
    global running
    log.info("Received signal %s, shutting down…", signum)
    running = False

signal.signal(signal.SIGINT, _shutdown)
signal.signal(signal.SIGTERM, _shutdown)

# ── Shared spike state (thread-safe via lock) ─────────────────────────────────

_spike_lock   = threading.Lock()
_spike_active = False
_last_spike   = time.time()


def _update_spike_state() -> bool:
    """Update and return whether we are currently in a spike window."""
    global _spike_active, _last_spike
    with _spike_lock:
        elapsed = time.time() - _last_spike
        if elapsed >= SPIKE_INTERVAL and not _spike_active:
            _spike_active = True
            log.warning("🔥 SPIKE STARTING — correlated errors on %s", SPIKE_SERVICES)
        elif _spike_active and elapsed >= SPIKE_INTERVAL + SPIKE_DURATION:
            _spike_active = False
            _last_spike   = time.time()
            log.info("✓ Spike ended")
        return _spike_active


def is_spike() -> bool:
    with _spike_lock:
        return _spike_active


# ── Helpers ───────────────────────────────────────────────────────────────────

def _rand_id() -> str:
    return uuid.uuid4().hex[:12]


def _fill_template(template: str, service: str) -> str:
    """Replace placeholders with realistic values."""
    return (
        template
        .replace("{svc}", service.split("-")[0])
        .replace("{uid}", _rand_id())
        .replace("{oid}", f"ORD-{_rand_id()[:8].upper()}")
        .replace("{sid}", _rand_id())
        .replace("{method}", random.choice(METHODS))
        .replace("{path}", random.choice(PATHS).replace("{oid}", _rand_id()[:8]).replace("{uid}", _rand_id()[:8]))
        .replace("{latency}", str(random.randint(2, 4500)))
        .replace("{dep}", random.choice(DEPENDENCIES))
        .replace("{metric}", random.choice(METRICS_LIST))
        .replace("{value}", f"{random.uniform(0.01, 99.9):.2f}")
        .replace("{minor}", str(random.randint(1, 42)))
        .replace("{patch}", str(random.randint(0, 99)))
        .replace("{amount}", f"{random.uniform(5, 500):.2f}")
    )


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


# ── Log generation ─────────────────────────────────────────────────────────────

def generate_log_entry(service: str, spike: bool) -> dict:
    weights = LEVEL_WEIGHTS_SPIKE if spike else LEVEL_WEIGHTS_NORMAL
    level   = random.choices(LEVELS, weights=weights, k=1)[0]

    templates = LOG_MESSAGES.get(level, LOG_MESSAGES["INFO"])
    message   = _fill_template(random.choice(templates), service)
    host      = random.choice(HOSTS).replace("{svc}", service.split("-")[0])

    trace_id = span_id = None
    if level in ("INFO", "WARN", "ERROR") and random.random() < 0.3:
        trace_id = uuid.uuid4().hex
        span_id  = uuid.uuid4().hex[:16]

    return {
        "timestamp": now_iso(),
        "level":     level,
        "service":   service,
        "host":      host,
        "message":   message,
        "trace_id":  trace_id,
        "span_id":   span_id,
        "attributes": {
            "env":    "production",
            "region": random.choice(["us-east-1", "us-west-2"]),
            "pod":    f"{service}-{_rand_id()[:6]}",
        },
    }


# ── Metric generation ──────────────────────────────────────────────────────────

def generate_metric_points(service: str, spike_active: bool) -> list[dict]:
    """Return one data point per tracked metric for the given service."""
    points = []
    for metric_name, (lo, hi) in METRIC_BASELINES.items():
        if spike_active and service in SPIKE_SERVICES:
            slo, shi = METRIC_SPIKES[metric_name]
            value = random.uniform(slo, shi)
        else:
            value = random.uniform(lo, hi)

        points.append({
            "metric_name": metric_name,
            "timestamp":   now_iso(),
            "value":       round(value, 4),
            "labels": {
                "service": service,
                "env":     "production",
                "region":  random.choice(["us-east-1", "us-west-2"]),
            },
        })
    return points


# ── HTTP helpers ───────────────────────────────────────────────────────────────

def send_logs(session: requests.Session, logs: list[dict]) -> bool:
    url = f"{BACKEND_URL}/internal/logs/ingest"
    try:
        resp = session.post(url, json={"logs": logs}, timeout=10)
        if resp.status_code == 200:
            log.debug("Logs batch: %d inserted", resp.json().get("inserted", 0))
            return True
        log.warning("Logs ingest %d: %s", resp.status_code, resp.text[:200])
        return False
    except requests.RequestException as exc:
        log.error("Logs send failed: %s", exc)
        return False


def send_metrics(session: requests.Session, metrics: list[dict]) -> bool:
    url = f"{BACKEND_URL}/internal/metrics/ingest"
    try:
        resp = session.post(url, json={"metrics": metrics}, timeout=10)
        if resp.status_code == 200:
            log.debug("Metrics batch: %d inserted", resp.json().get("inserted", 0))
            return True
        log.warning("Metrics ingest %d: %s", resp.status_code, resp.text[:200])
        return False
    except requests.RequestException as exc:
        log.error("Metrics send failed: %s", exc)
        return False


# ── Wait for backend ───────────────────────────────────────────────────────────

def wait_for_backend(session: requests.Session) -> None:
    for attempt in range(30):
        if not running:
            return
        try:
            r = session.get(f"{BACKEND_URL}/api/v1/ready", timeout=5)
            if r.status_code == 200:
                log.info("Backend is ready")
                return
        except requests.RequestException:
            pass
        log.info("Waiting for backend (attempt %d/30)…", attempt + 1)
        time.sleep(2)
    log.error("Backend did not become ready in time, starting anyway…")


# ── Log producer loop ─────────────────────────────────────────────────────────

def log_loop(session: requests.Session) -> None:
    total_sent = 0
    log.info("Log producer starting — rate: %d logs/s", LOG_RATE)
    while running:
        cycle_start = time.time()
        spike_now   = _update_spike_state()

        batch = []
        for _ in range(LOG_BATCH_SIZE):
            # During spike: 70% of log traffic concentrated on spike services
            if spike_now and random.random() < 0.7:
                service = random.choice(SPIKE_SERVICES)
            else:
                service = random.choice(SERVICES)
            batch.append(generate_log_entry(service, spike_now))

        if send_logs(session, batch):
            total_sent += len(batch)

        if total_sent % (LOG_RATE * 30) < LOG_BATCH_SIZE:
            status = "🔥 SPIKE" if spike_now else "✓ normal"
            log.info("Logs sent: %d [%s]", total_sent, status)

        elapsed = time.time() - cycle_start
        time.sleep(max(0, 1.0 - elapsed))

    log.info("Log producer stopped. Total sent: %d", total_sent)


# ── Metrics producer loop ─────────────────────────────────────────────────────

def metrics_loop(session: requests.Session) -> None:
    total_sent = 0
    log.info("Metrics producer starting — rate: %d points/s", METRICS_RATE)

    while running:
        cycle_start = time.time()
        spike_now   = is_spike()

        batch: list[dict] = []
        for service in SERVICES:
            batch.extend(generate_metric_points(service, spike_now))

        if send_metrics(session, batch):
            total_sent += len(batch)

        if total_sent % (METRICS_RATE * 60) < len(batch):
            status = "🔥 SPIKE" if spike_now else "✓ normal"
            log.info("Metrics sent: %d [%s]", total_sent, status)

        elapsed = time.time() - cycle_start
        time.sleep(max(0, 1.0 - elapsed))

    log.info("Metrics producer stopped. Total sent: %d", total_sent)


# ── Main ──────────────────────────────────────────────────────────────────────

def main():
    log.info(
        "Simulator starting — target: %s, log_rate: %d/s, metric_rate: %d/s, "
        "services: %s, spike_interval: %ds, spike_duration: %ds",
        BACKEND_URL, LOG_RATE, METRICS_RATE, SERVICES,
        SPIKE_INTERVAL, SPIKE_DURATION,
    )
    log.info("Correlated spike services: %s", SPIKE_SERVICES)

    if not TOKEN:
        log.warning("SRE_INTERNAL_TOKEN is not set — requests will likely fail auth")

    session = requests.Session()
    session.headers.update({
        "Authorization": f"Bearer {TOKEN}",
        "Content-Type":  "application/json",
    })

    wait_for_backend(session)

    # Run logs + metrics producers in parallel threads
    log_thread     = threading.Thread(target=log_loop, args=(session,), daemon=True)
    metrics_thread = threading.Thread(target=metrics_loop, args=(session,), daemon=True)

    log_thread.start()
    metrics_thread.start()

    # Block main thread until shutdown signal
    while running:
        time.sleep(0.5)

    log_thread.join(timeout=5)
    metrics_thread.join(timeout=5)
    log.info("Simulator shut down cleanly")


if __name__ == "__main__":
    main()
