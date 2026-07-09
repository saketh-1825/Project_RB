# Autonomous SRE Copilot

An autonomous site-reliability engineering assistant that ingests telemetry (logs, metrics, traces), detects anomalies, triggers AI-powered root-cause analysis via LangGraph, and surfaces results in a real-time dashboard with human-in-the-loop control.

---

## Architecture

```
Prometheus / Alertmanager
        │ webhook
        ▼
┌─────────────────────┐     REST/WS     ┌──────────────────────┐
│   Go Backend        │◄────────────────►   LangGraph Service   │
│   :8080             │                 │   :9000               │
│                     │                 │                       │
│  REST API           │                 │  supervisor agent     │
│  WebSocket hub      │                 │  log_query_agent      │
│  Webhook receivers  │                 │  rag_agent            │
│  Dashboard UI       │                 │  correlation_agent    │
└──────┬──────────────┘                 │  report_agent         │
       │                                └──────────────────────┘
       │
  ┌────┴────────┐
  │  PostgreSQL  │  (pgvector — logs, metrics, runbooks, incidents)
  │  Redis       │  (retry queue, session state)
  └─────────────┘

  Simulator → POST /internal/{logs,metrics}/ingest
  Dashboard → http://localhost:8080/dashboard/
```

---

## Prerequisites

| Tool | Min version |
|---|---|
| Docker + Docker Compose | Compose ≥ 2.20 |
| `openssl` | any (for HMAC test script) |
| `curl`, `python3` | any (for test script) |
| Go | 1.22+ (only for local dev without Docker) |
| Python | 3.11+ (only for local LangGraph dev) |

---

## First-time setup

```bash
# 1. Clone and enter the repo
cd Project_RB

# 2. Create your .env from the example
cp .env.example .env    # edit and fill in API keys + secrets

# Required variables:
#   POSTGRES_USER           — e.g. sreuser
#   POSTGRES_PASSWORD       — strong password
#   SRE_INTERNAL_TOKEN      — any random secret (shared between Go + LangGraph)
#   PROMETHEUS_WEBHOOK_SECRET — HMAC secret for Prometheus webhook auth
#   DATADOG_WEBHOOK_SECRET    — HMAC secret for Datadog webhook auth
#   CHROMA_TOKEN              — ChromaDB auth token
#   OPENROUTER_API_KEY        — Required for profile:full (LangGraph LLM calls)
#   OPENROUTER_MODEL          — e.g. anthropic/claude-3-5-haiku
```

---

## Running — `profile go` (Go backend dev)

Use this when you are working on the **Go backend**. The LangGraph service is replaced by a WireMock mock.

```bash
docker compose --profile go up --build
```

**What starts:**
| Service | Port | Notes |
|---|---|---|
| `go-backend` | 8080 | Real Go service with hot-reload (air) |
| `mock-langgraph` | 9000 | WireMock serving realistic LangGraph responses |
| `simulator` | — | Sends logs + metrics to Go backend |
| `postgres` | 5432 | pgvector-enabled |
| `redis` | 6379 | Retry queue |
| `prometheus` | 9090 | Fires real alerts at your webhook |
| `alertmanager` | 9093 | Routes alerts to Go webhook |
| `adminer` | 8888 | DB browser |

**Access:**
- Dashboard: http://localhost:8080/dashboard/
- API docs (summary): `curl -H "Authorization: Bearer $SRE_INTERNAL_TOKEN" http://localhost:8080/api/v1/dashboard/summary`
- Prometheus UI: http://localhost:9090
- Adminer: http://localhost:8888

**Trigger a test alert manually:**
```bash
./go_backend/scripts/test_contracts.sh
```

---

## Running — `profile full` (integration / pre-PR)

Use this to run **both real services** and verify end-to-end integration before merging.

```bash
docker compose --profile full up --build
```

**What starts:**
Everything in `profile go` + the real `langgraph-service` (port 9000).

> Requires `OPENROUTER_API_KEY` to be set — LangGraph makes real LLM calls.

**Run all 5 contract integration scenarios:**
```bash
chmod +x go_backend/scripts/test_contracts.sh
./go_backend/scripts/test_contracts.sh
# All 5 scenarios pass → green ✓
```

---

## Simulator

The simulator auto-starts as part of `profile go` and `profile full`. It runs two producers:

| Producer | Endpoint | Default rate |
|---|---|---|
| Log producer | `POST /internal/logs/ingest` | 20 logs/s |
| Metrics producer | `POST /internal/metrics/ingest` | 10 points/s |

**Spike mode:** Every `SPIKE_INTERVAL_SECONDS` (default 120 s), both `payment-api` and `order-service` simultaneously receive:
- 35–75% error rate
- p99 latency 2–9 s
- DB connection pool near capacity
- Queue depth 500–2000

Configure via docker-compose environment or `.env`:
```env
LOG_RATE_PER_SECOND=20
METRICS_RATE_PER_SECOND=10
SPIKE_INTERVAL_SECONDS=120
SPIKE_DURATION_SECONDS=10
```

---

## Key API Endpoints

All authenticated endpoints require `Authorization: Bearer $SRE_INTERNAL_TOKEN`.

| Endpoint | Description |
|---|---|
| `GET /api/v1/ready` | Readiness probe — 200 if all stores up, 503 otherwise |
| `GET /api/v1/health` | Component health (postgres, redis, vector_index) |
| `GET /api/v1/dashboard/summary` | Open incidents + firing alerts + recent analyses |
| `GET /api/v1/incidents` | List incidents (filter by status, severity, service) |
| `GET /api/v1/incidents/:id` | Full incident detail with report + timeline + findings |
| `POST /api/v1/incidents` | Create a new incident |
| `GET /api/v1/alerts` | List alerts (filter by status, severity) |
| `GET /api/v1/logs` | Query logs by time range, service, level, regex |
| `GET /api/v1/logs/anomalies` | Pre-computed anomalous windows |
| `GET /api/v1/metrics/query` | Time-series query |
| `POST /api/v1/metrics/query/batch` | Parallel batch metric query (max 20) |
| `GET /api/v1/runbooks/search` | Semantic runbook search |
| `POST /internal/logs/ingest` | Bulk log ingestion (simulator → Go) |
| `POST /internal/metrics/ingest` | Bulk metric ingestion (simulator → Go) |
| `POST /webhooks/prometheus` | Prometheus Alertmanager receiver (HMAC auth) |

---

## WebSocket

Connect to `ws://localhost:8080/ws` (optionally with `?token=...`).

**Server → Client events:**
- `alert.fired` — new firing alert
- `analysis.started` — LangGraph began investigating
- `analysis.agent_switched` — supervisor handoff between agents
- `analysis.finding` — intermediate finding from an agent
- `analysis.awaiting_human` — AI paused, needs your input
- `analysis.completed` — report ready
- `analysis.failed` — unrecoverable error
- `incident.updated` — incident metadata changed
- `ping` — respond with `pong`

**Client → Server events:**
- `subscribe.incident` — subscribe to granular updates for an incident
- `unsubscribe.incident` — unsubscribe
- `human_input` — send human decision to paused analysis

---

## Dashboard

Open http://localhost:8080/dashboard/ in your browser.

| Page | What it shows |
|---|---|
| Overview | Open incidents, firing alerts, recent analyses, live event feed |
| Incidents | Table of open incidents with severity/status badges |
| Alerts | Firing alerts from all sources |
| Analyses | LangGraph investigation runs |
| Incident detail | Full report: executive summary, root cause, suggested fixes (priority-ordered), findings timeline, incident event log |
| HITL action card | Appears automatically when `analysis.awaiting_human` fires — approve/reject fixes or provide context |

---

## Development tips

**Hot-reload Go (air):**
```bash
# Changes to go_backend/ are automatically recompiled by air inside Docker
# No rebuild needed during development
```

**Check readiness:**
```bash
curl http://localhost:8080/api/v1/ready
# {"ready":true}

# Or check with Redis down:
docker compose stop redis
curl http://localhost:8080/api/v1/ready
# {"ready":false,"reason":"redis connection failed: ..."}
docker compose start redis
```

**Watch live telemetry from simulator:**
```bash
docker compose logs -f simulator
```

**Run contract tests:**
```bash
chmod +x go_backend/scripts/test_contracts.sh
./go_backend/scripts/test_contracts.sh http://localhost:8080 "$SRE_INTERNAL_TOKEN"
```

---

## Directory structure

```
Project_RB/
├── go_backend/           # Go REST API + WebSocket server
│   ├── handlers/         # HTTP handler implementations
│   │   ├── dashboard.go  # GET /api/v1/dashboard/summary
│   │   ├── incidents.go  # /incidents/* + finding validation
│   │   ├── system.go     # /health + /ready (503 on store failure)
│   │   └── …
│   ├── db/               # PostgreSQL store implementations + migrations
│   ├── clients/          # LangGraph + Redis + Embedder HTTP clients
│   ├── ws/               # WebSocket hub (subscribe, human_input forwarding)
│   ├── dashboard/        # Embedded static dashboard (index.html, app.js, style.css)
│   ├── middleware/        # BearerAuth, HMACAuth, RequestID, StructuredLogger
│   └── scripts/
│       └── test_contracts.sh  # 5-scenario integration test
├── langgraph_service/    # Python LangGraph AI analysis pipeline
├── mocks/
│   ├── simulator/        # Telemetry simulator (logs + metrics + spike)
│   ├── go-backend/       # WireMock stubs for Go backend (AI dev profile)
│   └── langgraph/        # WireMock stubs for LangGraph (Go dev profile)
├── docker-compose.yml    # Multi-profile compose (go / ai / full)
├── prometheus.yml        # Prometheus scrape config
├── alert.rules.yml       # Prometheus alert rules
└── alertmanager.yml      # Routes alerts to Go webhook
```
