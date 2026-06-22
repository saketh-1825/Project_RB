#!/usr/bin/env python3
"""
Telemetry Simulator — generates realistic structured logs and POSTs them
to the Go backend's /internal/logs/ingest endpoint in batches.

Environment variables (set via docker-compose):
    GO_BACKEND_URL          — e.g. http://go-backend:8080
    SRE_INTERNAL_TOKEN      — Bearer token for auth
    SERVICES                — comma-separated list of service names
    LOG_RATE_PER_SECOND     — target log lines per second  (default 20)
    ERROR_RATE              — baseline error fraction       (default 0.04)
    SPIKE_INTERVAL_SECONDS  — inject error spike every N s  (default 120)
"""

import json
import logging
import os
import random
import signal
import sys
import time
import uuid
from datetime import datetime, timezone

import requests

# ── Configuration ─────────────────────────────────────────────────────────────

BACKEND_URL = os.getenv("GO_BACKEND_URL", "http://go-backend:8080")
TOKEN = os.getenv("SRE_INTERNAL_TOKEN", "")
SERVICES = os.getenv("SERVICES", "payment-api,auth-service,order-service,notification-service,api-gateway").split(",")
LOG_RATE = int(os.getenv("LOG_RATE_PER_SECOND", "20"))
ERROR_RATE = float(os.getenv("ERROR_RATE", "0.04"))
SPIKE_INTERVAL = int(os.getenv("SPIKE_INTERVAL_SECONDS", "120"))
BATCH_SIZE = max(LOG_RATE, 20)  # flush once per second

# ── Realistic log templates ──────────────────────────────────────────────────

LEVELS = ["DEBUG", "INFO", "INFO", "INFO", "WARN", "ERROR", "FATAL"]
LEVEL_WEIGHTS_NORMAL = [5, 50, 50, 50, 10, 4, 1]        # ~2.4% error+fatal
LEVEL_WEIGHTS_SPIKE = [1, 10, 10, 10, 15, 40, 14]        # ~54% error+fatal

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
        "Processed order {oid} for user {uid}, total=${ amount}",
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

METHODS = ["GET", "POST", "PUT", "PATCH", "DELETE"]
PATHS = [
    "/api/v1/orders", "/api/v1/orders/{oid}",
    "/api/v1/payments", "/api/v1/payments/{oid}/capture",
    "/api/v1/users/{uid}", "/api/v1/users/{uid}/profile",
    "/api/v1/auth/login", "/api/v1/auth/refresh",
    "/api/v1/notifications/send", "/api/v1/health",
]
METRICS = [
    "http_request_duration_seconds", "db_pool_active_connections",
    "order_processing_duration_ms", "cache_hit_ratio",
    "queue_depth", "error_rate_1m",
]
DEPENDENCIES = ["postgres", "redis", "kafka", "auth-service", "payment-gateway", "order-service"]

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


# ── Helpers ───────────────────────────────────────────────────────────────────

def _rand_id():
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
        .replace("{metric}", random.choice(METRICS))
        .replace("{value}", f"{random.uniform(0.01, 99.9):.2f}")
        .replace("{minor}", str(random.randint(1, 42)))
        .replace("{patch}", str(random.randint(0, 99)))
        .replace("{ amount}", f"{random.uniform(5, 500):.2f}")
    )


def generate_log_entry(service: str, is_spike: bool) -> dict:
    """Generate a single realistic log entry."""
    weights = LEVEL_WEIGHTS_SPIKE if is_spike else LEVEL_WEIGHTS_NORMAL
    level = random.choices(LEVELS, weights=weights, k=1)[0]

    templates = LOG_MESSAGES.get(level, LOG_MESSAGES["INFO"])
    message = _fill_template(random.choice(templates), service)
    host = random.choice(HOSTS).replace("{svc}", service.split("-")[0])

    # ~30 % of INFO/WARN logs carry a trace_id
    trace_id = None
    span_id = None
    if level in ("INFO", "WARN", "ERROR") and random.random() < 0.3:
        trace_id = uuid.uuid4().hex
        span_id = uuid.uuid4().hex[:16]

    return {
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "level": level,
        "service": service,
        "host": host,
        "message": message,
        "trace_id": trace_id,
        "span_id": span_id,
        "attributes": {
            "env": "production",
            "region": random.choice(["us-east-1", "us-west-2"]),
            "pod": f"{service}-{_rand_id()[:6]}",
        },
    }


def send_batch(session: requests.Session, logs: list[dict]) -> bool:
    """POST a batch of logs to the ingestion endpoint. Returns True on success."""
    url = f"{BACKEND_URL}/internal/logs/ingest"
    try:
        resp = session.post(url, json={"logs": logs}, timeout=10)
        if resp.status_code == 200:
            data = resp.json()
            log.debug("Batch sent: %d/%d inserted", data.get("inserted", 0), len(logs))
            return True
        else:
            log.warning("Ingest returned %d: %s", resp.status_code, resp.text[:200])
            return False
    except requests.RequestException as exc:
        log.error("Failed to send batch: %s", exc)
        return False


# ── Main loop ─────────────────────────────────────────────────────────────────

def main():
    log.info("Simulator starting — target: %s, rate: %d logs/s, services: %s",
             BACKEND_URL, LOG_RATE, SERVICES)

    if not TOKEN:
        log.warning("SRE_INTERNAL_TOKEN is not set — requests will likely fail auth")

    session = requests.Session()
    session.headers.update({
        "Authorization": f"Bearer {TOKEN}",
        "Content-Type": "application/json",
    })

    # Wait for backend to become ready
    for attempt in range(30):
        if not running:
            return
        try:
            r = session.get(f"{BACKEND_URL}/api/v1/ready", timeout=5)
            if r.status_code == 200:
                log.info("Backend is ready")
                break
        except requests.RequestException:
            pass
        log.info("Waiting for backend (attempt %d/30)…", attempt + 1)
        time.sleep(2)
    else:
        log.error("Backend did not become ready in time, starting anyway…")

    last_spike = time.time()
    total_sent = 0
    interval = 1.0 / max(LOG_RATE, 1)

    while running:
        cycle_start = time.time()

        # Determine if we're in a spike window (lasts 10 seconds)
        elapsed_since_spike = time.time() - last_spike
        is_spike = elapsed_since_spike >= SPIKE_INTERVAL and elapsed_since_spike < SPIKE_INTERVAL + 10
        if elapsed_since_spike >= SPIKE_INTERVAL + 10:
            last_spike = time.time()

        # Generate a batch
        batch = []
        for _ in range(BATCH_SIZE):
            service = random.choice(SERVICES)
            # During a spike, concentrate errors on 1-2 services
            if is_spike and random.random() < 0.7:
                service = SERVICES[0]  # payment-api gets hammered
            batch.append(generate_log_entry(service, is_spike))

        if send_batch(session, batch):
            total_sent += len(batch)

        if total_sent % (LOG_RATE * 30) < BATCH_SIZE:
            status = "🔥 SPIKE" if is_spike else "✓ normal"
            log.info("Total logs sent: %d [%s]", total_sent, status)

        # Sleep to maintain target rate
        elapsed = time.time() - cycle_start
        sleep_time = max(0, 1.0 - elapsed)
        if sleep_time > 0:
            time.sleep(sleep_time)

    log.info("Simulator stopped. Total logs sent: %d", total_sent)


if __name__ == "__main__":
    main()
