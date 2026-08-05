# Autonomous SRE Copilot

An autonomous **Site Reliability Engineering assistant** that ingests real-time telemetry (logs, metrics, traces), detects anomalies, triggers AI-powered root-cause analysis via a **LangGraph multi-agent pipeline**, and surfaces results in a live dashboard with **human-in-the-loop** control.

---

## What This Project Is

When a production alert fires (from Prometheus, Datadog, or any webhook source), this system automatically:

1. **Receives** the alert webhook in the Go backend
2. **Creates** an incident ticket and broadcasts it to all dashboard subscribers via WebSocket
3. **Triggers** the LangGraph AI pipeline to investigate the root cause
4. **Collects evidence** in parallel — logs, metrics, runbooks, service topology
5. **Correlates** evidence to identify the root cause with a confidence score
6. **Pauses for human input** if confidence is low, or if runbook similarity is below threshold
7. **Generates** a structured incident report with an executive summary, timeline, root cause, and suggested fixes
8. **Pushes** the report back to the Go backend, which broadcasts the final result to the dashboard

The entire investigation — from alert to report — typically completes in under 5 minutes without any human action required, unless a low-confidence situation triggers the HITL interrupt.

---

## System Architecture

```
Prometheus / Alertmanager / Datadog
         │ webhook (HMAC-signed)
         ▼
┌──────────────────────────────────────┐        REST         ┌─────────────────────────────────┐
│          Go Backend  :8080           │◄────────────────────►│   LangGraph Service  :9000      │
│                                      │                      │                                 │
│  REST API (Gin)                      │  POST /api/v1/       │  FastAPI + LangGraph            │
│  WebSocket Hub                       │  analyses            │  Multi-agent graph              │
│  Webhook Receivers                   │                      │  WebSocket event stream         │
│  Dashboard (embedded static UI)      │◄────────────────────►│  Redis state persistence        │
│                                      │  PATCH /incidents    │                                 │
└──────────┬───────────────────────────┘                      └─────────────────────────────────┘
           │
    ┌──────┴──────────────┐
    │  PostgreSQL (pg16)   │  pgvector — logs, metrics, traces, runbooks, incidents, analyses
    │  Redis 7.2           │  retry queue, analysis state, event log (LPUSH lists)
    │  ChromaDB            │  vector embeddings for semantic runbook search
    └─────────────────────┘

Simulator → POST /internal/{logs,metrics}/ingest  (20 logs/s, 10 metrics/s)
Dashboard → http://localhost:8080/dashboard/
```

---

## Component Breakdown

### Go Backend (`go_backend/`)

Written in **Go 1.25** with the **Gin** HTTP framework. This is the central hub of the system.

| Package | Responsibility |
|---|---|
| `handlers/` | HTTP handler implementations for every REST endpoint |
| `handlers/webhooks.go` | Receives Prometheus, Datadog, and custom webhook payloads; saves alerts; triggers LangGraph |
| `handlers/incidents.go` | CRUD for incidents; finding validation |
| `handlers/dashboard.go` | `GET /api/v1/dashboard/summary` — aggregates open incidents, firing alerts, recent analyses |
| `handlers/system.go` | `/health` and `/ready` — returns 503 when any store is down |
| `db/` | PostgreSQL store implementations (alerts, logs, metrics, traces, services, runbooks, incidents, analyses) using `pgx/v5` |
| `db/migrate.go` | `golang-migrate` runner — auto-migrates schema on startup |
| `clients/langgraph.go` | HTTP client for calling LangGraph `/api/v1/analyses` |
| `clients/redis.go` | Redis client wrapper |
| `clients/retry_queue.go` | Redis-backed retry queue — enqueues failed LangGraph calls for re-delivery |
| `ws/hub.go` | WebSocket hub — fan-out broadcaster for all real-time events |
| `middleware/` | BearerAuth, HMACAuth (Prometheus/Datadog webhook signing), RequestID, StructuredLogger (zerolog) |
| `dashboard/` | Embedded static SPA (index.html, app.js, style.css) served at `/dashboard/` |
| `app.go` | Dependency injection — wires all stores, handlers, clients into the `App` struct |
| `main.go` | Router setup (Gin), HTTP server lifecycle, graceful shutdown |

**Alert → LangGraph Flow (Go side):**
```
Webhook received
  └─► HMAC signature verified (middleware)
      └─► Alert saved to PostgreSQL (alertStore.Save)
          └─► hub.BroadcastEvent("alert.fired")     ← all WS clients notified
              └─► go triggerAnalysis(alert)          ← goroutine, non-blocking
                    └─► langGraphClient.TriggerAnalysis(ctx, req)
                          ├─► Success: hub.BroadcastEvent("analysis.started")
                          └─► Failure: retryQueue.Enqueue(req)  ← Redis retry
```

---

### LangGraph Service (`langgraph_service/`)

Written in **Python 3.11** using **FastAPI** + **LangGraph**. This is the AI reasoning engine.

| Module | Responsibility |
|---|---|
| `main.py` | FastAPI app — `/api/v1/analyses`, `/api/v1/health`, `/ws/analysis/{id}`, `/dashboard/{id}` |
| `workflow/graph.py` | LangGraph `StateGraph` definition — nodes, edges, conditional routing, `run_analysis()`, `resume_analysis()` |
| `agents/supervisor.py` | First node — initializes state, loads topology, creates incident in Go backend |
| `agents/evidence_agent.py` | Orchestration layer — runs log, RAG, metrics, topology collectors in **parallel** |
| `agents/log_query_agent.py` | Queries Go backend for ERROR/FATAL logs in a 10-minute window |
| `agents/rag_agent.py` | Semantic runbook search via Go backend; triggers HITL pause if similarity < 0.7 |
| `agents/correlation_agent.py` | Correlates all evidence, calculates confidence score, infers root cause |
| `agents/human_review_agent.py` | Dedicated HITL node — halts graph when confidence < 0.75, waits for operator input |
| `agents/report_agent.py` | Builds final incident report (executive summary, timeline, root cause, suggested fixes) |
| `internal/analysis_coordinator.py` | Scans Redis for related in-flight analyses; links overlapping alerts |
| `internal/graph_events.py` | Emits structured events (LPUSH to Redis) + broadcasts to WebSocket subscribers |
| `internal/redis_client.py` | Redis client; `save_analysis_state()` persists full graph state |
| `internal/websocket_manager.py` | WebSocket connection manager; replays event history on reconnect |
| `internal/correlation/engine.py` | Deterministic root cause inference engine; spike detection; historical matching |
| `api/routes/interrupt.py` | `POST /api/v1/analyses/{id}/interrupt` — receives human decisions to resume paused graphs |
| `schemas/state.py` | `AnalysisState` TypedDict — single shared state object passed through all graph nodes |

---

## LangGraph Agent Flow

The graph is compiled from a `StateGraph(AnalysisState)` with the following node topology:

```
START
  │
  ▼
[supervisor]
  │  - Initializes state (findings=[], incident_events=[], resume_count=0)
  │  - Health-checks Go backend
  │  - Loads service topology from Go backend
  │  - Creates incident ticket in Go backend (stores incident_id in state)
  │  - Sets status="running"
  │
  ▼
[analysis_coordinator]
  │  - Scans Redis SCAN for all active/recent analyses (pattern: analysis:*)
  │  - Checks if any existing analysis shares a service or topology dependency
  │  - Populates state["related_analyses"] with linked investigation IDs
  │
  ▼
[evidence_agent]  ← parallel evidence collection via asyncio.gather()
  ├── log_query_agent    Fetches ERROR/FATAL logs for affected_services in ±10 min window
  ├── rag_agent          Semantic runbook search; pauses for HITL if similarity < 0.7
  ├── metrics_collector  Batch queries: error_rate, cpu, memory, db_pool_waiting
  └── topology_collector Loads or reuses services_topology from state
  │
  │  All 4 collectors run concurrently. Results are merged into state["evidence"]
  │
  ├─► IF rag_agent set status="awaiting_human"
  │      └─► route_after_evidence → END  (graph halts, waits for /interrupt call)
  │
  └─► OTHERWISE → [correlation_agent]
        │  - Reads state["evidence"] (logs, metrics, rag, topology)
        │  - Calls infer_root_cause() — deterministic spike/pattern matching
        │  - Calls calculate_confidence() — weighted scoring:
        │      +0.30 strong log anomaly (log_ids present)
        │      +0.30 metric anomaly (spike detected or non-UNKNOWN root cause)
        │      +0.20 runbook similarity ≥ 0.70 from RAG
        │      +0.20 topology data available
        │      +0.30 human_context provided (post-HITL boost)
        │      max score capped at 1.0
        │  - Calls calculate_risk() and analyze_evidence_quality()
        │  - Stores full correlation object in state["correlation"]
        │  - Posts correlation finding to Go backend (incident_id)
        │
        ├─► confidence_router:
        │     IF backend_health == "unavailable"  → [report_agent]  (skip review)
        │     IF confidence.score >= 0.75         → [report_agent]
        │     IF confidence.score < 0.75          → [human_review]
        │
        ├─► [human_review]
        │     - Sets status="awaiting_human", waiting_at="confidence_review"
        │     - Sets interrupt_question and requires_input=True
        │     → END  (graph halts; human sends context via /interrupt)
        │
        └─► [report_agent]
              - build_timeline(): chronological events from metric spikes → alert fired → findings → now
              - build_root_cause(): consumes state["root_cause"], aggregates supporting findings
              - extract_runbook_fixes(): sorts runbooks by similarity_score, assigns HIGH/MEDIUM/LOW priority
              - build_executive_summary(): dynamic narrative from services + confidence + evidence
              - build_incident_report(): final IncidentReport dict
              - Submits report to Go backend via client.submit_report(incident_id, report)
              - Sets status="completed"
              → END
```

### Human-in-the-Loop (HITL) Resume Flow

When the graph pauses (at `rag_agent` or `human_review`), the dashboard shows an action card:

```
Operator sees: interrupt_question + collected evidence so far
Operator submits: human decision or context string

POST /api/v1/analyses/{analysis_id}/interrupt
  └─► resume_analysis(state) called in graph.py
        └─► state["resume_count"] += 1
            IF resume_count > 2 → status="failed", graph ends
            ELSE:
              - state["human_context"] = operator input
              - state["status"] = "running", state["awaiting_human"] = False
              - PREVIOUS_NODE lookup: waiting_at → as_node for graph.update_state()
              - graph_with_checkpoint.invoke(None, config) resumes from correct node
                └─► rag_agent re-runs with human_context now set
                    └─► correlation_agent picks up +0.30 human context boost
```

### Event Emission

Every node transition emits structured events that flow to all WebSocket subscribers:

| Event Type | When |
|---|---|
| `analysis.started` | `run_analysis()` begins |
| `analysis.agent_switched` (running) | Node starts executing |
| `analysis.finding` | New finding discovered during node execution |
| `analysis.agent_switched` (completed/failed) | Node finishes |
| `analysis.agent_switched` (skipped) | Node was not reached — emitted at graph end |
| `analysis.awaiting_human` | Graph paused for operator input |
| `analysis.completed` | `report_agent` finished, status=completed |
| `analysis.failed` | Unrecoverable error or max resumptions exceeded |

Events are **LPUSH**ed to Redis key `analysis:{id}:events` and broadcast via WebSocket. On reconnect, the WebSocket manager replays the full history so late-joining clients see the complete timeline.

---

## State Object (`AnalysisState`)

The single TypedDict passed through every node:

```python
class AnalysisState(TypedDict, total=False):
    # Core
    analysis_id: str
    alert_id: str
    alert: dict                      # raw alert payload
    incident_id: Optional[str]       # created by supervisor in Go backend
    status: str                      # pending / running / awaiting_human / completed / failed
    current_agent: str

    # Evidence
    findings: List[Dict]             # accumulated findings from all agents
    incident_events: List[Dict]      # timeline events
    evidence: Optional[dict]         # unified evidence object (logs, metrics, rag, topology)
    metrics_data: Optional[dict]
    metrics_summary: Optional[dict]
    services_topology: Optional[dict]
    similar_incidents: Optional[List]

    # Correlation
    root_cause: Optional[dict]       # type, description, confidence, affected_services
    correlation: Optional[dict]      # full correlation output with confidence, risk, quality
    correlation_finding: Optional[dict]
    evidence_quality: Optional[dict]
    risk_assessment: Optional[dict]

    # RAG
    rag_query: str
    incident_title: str
    incident_summary: str

    # HITL
    awaiting_human: bool
    waiting_at: Optional[str]        # which node paused
    interrupt_type: Optional[str]
    interrupt_question: Optional[str]
    human_context: Optional[str]     # operator-provided context
    resume_count: int                # max 2 resumptions before failing
    last_interrupted_at: Optional[str]

    # Human Review Node
    review_reason: Optional[str]
    requires_input: Optional[bool]

    # Analysis Coordinator
    related_analyses: Optional[List[dict]]  # linked overlapping alerts

    # Output
    report: Optional[dict]           # final IncidentReport
    backend_health: Optional[str]    # "ok" or "unavailable"
```

---

## Development Profiles

The `docker-compose.yml` has three named profiles designed to isolate concerns:

| Profile | Go Backend | LangGraph | Purpose |
|---|---|---|---|
| `go` | **Real** | WireMock mock | Develop Go backend without needing OpenRouter API key |
| `ai` | WireMock mock | **Real** | Develop LangGraph pipeline without running Go locally |
| `full` | **Real** | **Real** | Pre-PR integration test — full end-to-end |

### Go dev (default daily workflow)
```bash
docker compose --profile go up --build
```

Starts: `go-backend` (hot-reload via air), `mock-langgraph` (WireMock on :9000), `postgres`, `redis`, `chroma`, `prometheus`, `alertmanager`, `simulator`, `adminer`

### AI / LangGraph dev
```bash
docker compose --profile ai up --build
```

Starts: `langgraph-service` (uvicorn --reload), `mock-go-backend` (WireMock on :8080), `postgres`, `redis`, `chroma`, `adminer`

Requires `OPENROUTER_API_KEY` in `.env`.

### Full integration
```bash
docker compose --profile full up --build
```

Runs everything real. Use before every PR merge.

---

## Prerequisites

| Tool | Version |
|---|---|
| Docker + Docker Compose | Compose ≥ 2.20 |
| `openssl` | any (for HMAC test script) |
| `curl`, `python3` | any (for test scripts) |
| Go | 1.22+ (local dev only) |
| Python | 3.11+ (local dev only) |

---

## First-Time Setup

```bash
cd Project_RB

cp .env.example .env   # fill in the following:

# POSTGRES_USER               — e.g. sreuser
# POSTGRES_PASSWORD           — strong password
# SRE_INTERNAL_TOKEN          — shared secret between Go and LangGraph
# PROMETHEUS_WEBHOOK_SECRET   — HMAC secret for Prometheus webhook auth
# DATADOG_WEBHOOK_SECRET      — HMAC secret for Datadog webhook auth
# CHROMA_TOKEN                — ChromaDB auth token
# OPENROUTER_API_KEY          — Required for profile:full and profile:ai
# OPENROUTER_MODEL            — e.g. anthropic/claude-3-5-haiku

docker compose --profile go up --build
```

---

## Simulator

Auto-starts with `go` and `full` profiles. Continuously generates realistic telemetry:

| Producer | Endpoint | Rate |
|---|---|---|
| Log producer | `POST /internal/logs/ingest` | 20 logs/s |
| Metrics producer | `POST /internal/metrics/ingest` | 10 points/s |

**Spike mode:** Every `SPIKE_INTERVAL_SECONDS` (default 120s), `payment-api` and `order-service` simultaneously receive:
- 35–75% error rate
- p99 latency 2–9s
- DB connection pool near capacity
- Queue depth 500–2000

Spike duration is 10 seconds. This triggers real Prometheus alert rules which fire to the webhook.

---

## Key API Endpoints

All authenticated endpoints require `Authorization: Bearer $SRE_INTERNAL_TOKEN`.

| Endpoint | Description |
|---|---|
| `GET /api/v1/ready` | Readiness probe — 200 if all stores up, 503 otherwise |
| `GET /api/v1/health` | Component health check (postgres, redis, vector_index) |
| `GET /api/v1/dashboard/summary` | Open incidents + firing alerts + recent analyses |
| `GET /api/v1/incidents` | List incidents (filter: status, severity, service) |
| `GET /api/v1/incidents/:id` | Full incident detail with report, timeline, findings |
| `POST /api/v1/incidents` | Create a new incident |
| `GET /api/v1/alerts` | List alerts (filter: status, severity) |
| `GET /api/v1/logs` | Query logs by time range, service, level, regex |
| `GET /api/v1/logs/anomalies` | Pre-computed anomalous windows |
| `GET /api/v1/metrics/query` | Time-series query |
| `POST /api/v1/metrics/query/batch` | Parallel batch metric query (max 20) |
| `GET /api/v1/runbooks/search` | Semantic runbook search |
| `POST /internal/logs/ingest` | Bulk log ingestion (simulator → Go) |
| `POST /internal/metrics/ingest` | Bulk metric ingestion (simulator → Go) |
| `POST /webhooks/prometheus` | Prometheus Alertmanager receiver (HMAC auth) |
| `POST /webhooks/datadog` | Datadog webhook receiver (HMAC auth) |
| `POST /webhooks/custom` | Generic webhook receiver |

---

## WebSocket

Connect to `ws://localhost:8080/ws` (optionally `?token=...`).

**Server → Client events:**
- `alert.fired` — new firing alert received
- `analysis.started` — LangGraph began investigating
- `analysis.agent_switched` — supervisor handoff between agents (includes running/completed/failed/skipped status)
- `analysis.finding` — intermediate finding from an agent
- `analysis.awaiting_human` — AI paused, needs operator input
- `analysis.completed` — final report ready
- `analysis.failed` — unrecoverable error or max resumptions exceeded
- `incident.updated` — incident metadata changed
- `ping` — keepalive, respond with `pong`

**Client → Server events:**
- `subscribe.incident` — subscribe to granular updates for a specific incident
- `unsubscribe.incident` — unsubscribe
- `human_input` — send operator decision to a paused analysis

---

## Dashboard

Open `http://localhost:8080/dashboard/` in your browser.

| Page | What it shows |
|---|---|
| Overview | Open incidents, firing alerts, recent analyses, live event feed |
| Incidents | Table of open incidents with severity/status badges |
| Alerts | All firing alerts from all sources |
| Analyses | LangGraph investigation runs and their current agent |
| Incident detail | Full report: executive summary, root cause, suggested fixes (priority-ordered), findings timeline, incident event log |
| HITL action card | Appears automatically when `analysis.awaiting_human` fires — approve/reject fixes or provide context to the paused graph |

The LangGraph service also exposes a **live agent timeline dashboard** at:
`http://localhost:9000/dashboard/{analysis_id}` — shows the real-time node execution timeline via WebSocket.

---

## Development Tips

**Hot-reload (Go):**
```bash
# air watches go_backend/ inside Docker — no rebuild needed
docker compose logs -f go-backend
```

**Hot-reload (Python):**
```bash
# uvicorn --reload watches langgraph_service/ inside Docker
docker compose logs -f langgraph-service
```

**Check readiness:**
```bash
curl http://localhost:8080/api/v1/ready
# {"ready":true}

# Test with Redis down:
docker compose stop redis
curl http://localhost:8080/api/v1/ready
# {"ready":false,"reason":"redis connection failed: ..."}
docker compose start redis
```

**Run contract tests (5 scenarios):**
```bash
chmod +x go_backend/scripts/test_contracts.sh
./go_backend/scripts/test_contracts.sh http://localhost:8080 "$SRE_INTERNAL_TOKEN"
# All 5 scenarios → green ✓
```

**Watch live simulator telemetry:**
```bash
docker compose logs -f simulator
```

**Trigger a manual test alert:**
```bash
./go_backend/scripts/test_contracts.sh
```

**Inspect database:**
Open `http://localhost:8888` — Adminer pointing at PostgreSQL.

---

## Directory Structure

```
Project_RB/
├── go_backend/                    # Go REST API + WebSocket server
│   ├── app.go                     # Dependency injection / App struct
│   ├── main.go                    # Router, server lifecycle
│   ├── config.go                  # Config loaded from env
│   ├── handlers/                  # HTTP handlers (alerts, logs, metrics, incidents, webhooks, dashboard, system)
│   ├── db/                        # PostgreSQL stores + migration runner
│   │   └── migrations/            # SQL migration files
│   ├── clients/                   # LangGraph, Redis, Embedder, RetryQueue clients
│   ├── ws/                        # WebSocket hub (subscribe, broadcast, human_input forwarding)
│   ├── middleware/                 # BearerAuth, HMACAuth, RequestID, StructuredLogger
│   ├── models/                    # Domain models (Alert, Incident, Log, Metric, etc.)
│   ├── dashboard/                 # Embedded static SPA (index.html, app.js, style.css)
│   └── scripts/
│       └── test_contracts.sh      # 5-scenario integration test script
│
├── langgraph_service/             # Python LangGraph AI analysis pipeline
│   ├── main.py                    # FastAPI app entry point
│   ├── workflow/
│   │   └── graph.py               # StateGraph definition, run_analysis(), resume_analysis()
│   ├── agents/                    # All agent node implementations
│   │   ├── supervisor.py
│   │   ├── evidence_agent.py      # Parallel evidence orchestration
│   │   ├── log_query_agent.py
│   │   ├── rag_agent.py
│   │   ├── correlation_agent.py
│   │   ├── human_review_agent.py
│   │   ├── report_agent.py
│   │   └── helpers.py
│   ├── schemas/
│   │   └── state.py               # AnalysisState TypedDict
│   ├── internal/
│   │   ├── analysis_coordinator.py  # Overlapping alert detection via Redis SCAN
│   │   ├── graph_events.py          # Event emission to Redis + WebSocket
│   │   ├── redis_client.py          # Redis client + state persistence
│   │   ├── websocket_manager.py     # WS connection manager + history replay
│   │   ├── correlation/
│   │   │   └── engine.py            # Root cause inference, spike detection, historical matching
│   │   └── client/
│   │       └── go_backend.py        # HTTP client for Go backend API
│   ├── api/routes/
│   │   └── interrupt.py             # POST /api/v1/analyses/{id}/interrupt
│   └── prompts/                     # Prompt templates (rag_query_prompt.txt, etc.)
│
├── mocks/
│   ├── simulator/                 # Telemetry simulator (logs + metrics + spike mode)
│   ├── go-backend/                # WireMock stubs — realistic Go backend responses (AI dev profile)
│   └── langgraph/                 # WireMock stubs — realistic LangGraph responses (Go dev profile)
│
├── docs/langgraph/                # LangGraph-specific documentation
├── docker-compose.yml             # Multi-profile compose (go / ai / full)
├── prometheus.yml                 # Prometheus scrape config
├── alert.rules.yml                # Prometheus alert rules
├── alertmanager.yml               # Routes Prometheus alerts to Go webhook
├── init.sql                       # PostgreSQL initial schema
└── sre_copilot_contract.json      # Full REST/WebSocket/Webhook API contract spec
```

---

## Environment Variables

| Variable | Used by | Purpose |
|---|---|---|
| `POSTGRES_USER` / `POSTGRES_PASSWORD` | Go, LangGraph | Database credentials |
| `POSTGRES_DSN` | Go | Full DSN (auto-built in compose) |
| `REDIS_URL` | Go, LangGraph | Redis connection string |
| `CHROMA_URL` / `CHROMA_TOKEN` | Go, LangGraph | ChromaDB vector store |
| `SRE_INTERNAL_TOKEN` | Both | Shared bearer token for service-to-service auth |
| `PROMETHEUS_WEBHOOK_SECRET` | Go | HMAC secret for Prometheus webhook verification |
| `DATADOG_WEBHOOK_SECRET` | Go | HMAC secret for Datadog webhook verification |
| `LANGGRAPH_URL` | Go | URL to call LangGraph (mock or real) |
| `GO_BACKEND_URL` | LangGraph | URL to call Go backend (mock or real) |
| `OPENROUTER_API_KEY` | LangGraph | LLM API key (required for profile:ai / profile:full) |
| `OPENROUTER_MODEL` | LangGraph | Model name (e.g. `anthropic/claude-3-5-haiku`) |
| `MAX_CONCURRENT_ANALYSES` | LangGraph | Concurrency limit (default: 3) |
| `ANALYSIS_TIMEOUT_SECONDS` | LangGraph | Per-analysis timeout (default: 300s) |
| `HUMAN_INTERRUPT_TIMEOUT_SECONDS` | LangGraph | HITL wait timeout (default: 120s) |
| `LOG_RATE_PER_SECOND` | Simulator | Log ingestion rate (default: 20) |
| `METRICS_RATE_PER_SECOND` | Simulator | Metrics ingestion rate (default: 10) |
| `SPIKE_INTERVAL_SECONDS` | Simulator | Seconds between synthetic spikes (default: 120) |
| `SPIKE_DURATION_SECONDS` | Simulator | Duration of each spike (default: 10) |
