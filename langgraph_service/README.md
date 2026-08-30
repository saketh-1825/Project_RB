# LangGraph SRE Copilot — AI Investigation Service

The reasoning layer of an autonomous incident response system. When a production alert fires, this service launches a multi-agent AI pipeline that collects evidence, correlates findings, scores confidence, and generates a complete incident report — without a human having to open a single dashboard manually.

Built with **FastAPI**, **LangGraph**, and **Redis**. Communicates with a Go backend over HTTP and streams every investigation step live to a browser dashboard via WebSocket.

---

## What problem does this solve?

When a production system breaks at 2 AM, an on-call engineer has to manually read the alert, pull logs from one tool, check metrics in another, cross-reference runbooks, and write up a report — a process that takes 30–90 minutes under pressure.

This service does all of that automatically. It only involves a human at the one point where AI confidence is genuinely insufficient.

---

## Architecture

```
[Alert Source: Prometheus / Datadog]
        │ HTTP POST (HMAC-signed)
        ▼
[Go Backend :8080]
  ├── Saves alert to PostgreSQL
  ├── Broadcasts alert.fired via WebSocket
  └── POST /api/v1/analyses ──────────────────────────────▶ [LangGraph Service :9000]
                                                                    │
                                                      Runs multi-agent graph
                                                                    │
                                                      PATCH /api/v1/incidents/{id}/report
                                                                    │
[Dashboard ◀── WebSocket events ◀──────────────────────────────────┘
 (live timeline, confidence score, findings, report)]
```

The Go backend handles infrastructure (webhooks, PostgreSQL, WebSocket hub, dashboard serving). This service handles reasoning (agent orchestration, evidence correlation, report generation).

---

## The Investigation Pipeline

Every alert triggers a **directed graph of 6 agents**, each with one job:

```
supervisor → evidence_agent → correlation_agent → report_agent
                                                            │
                                                     (if confidence < 0.75)
                                                            ▼
                                                      human_review  ──▶  [paused]
                                                            │
                                                     (human resumes)
                                                            ▼
                                                      correlation_agent → report_agent
```

| Agent | What it does |
|---|---|
| **supervisor** | Initialises state, health-checks Go backend, loads service topology, creates incident ticket |

| **evidence_agent** | Runs 4 collectors in parallel: logs, metrics, RAG runbook search, topology |
| **correlation_agent** | Scores confidence deterministically, infers root cause, assesses risk |
| **human_review** | Pauses the graph and asks the on-call engineer for context (only triggered when confidence < 0.75) |
| **report_agent** | Builds timeline, root cause summary, suggested fixes, and executive summary — submits to Go backend |

---

## Confidence Scoring

The correlation agent scores evidence deterministically — no LLM, no guessing. The same inputs always produce the same score.

| Evidence | Points |
|---|---|
| Log anomaly with real log IDs | +0.30 |
| Metric anomaly or known root cause type | +0.30 |
| Matching runbook (similarity ≥ 0.70) | +0.20 |
| Service topology available | +0.20 |
| Human context provided (post-HITL) | +0.30 |
| **Maximum** | **1.0** |

**Threshold: 0.75** — above this, the system completes autonomously. Below it, the graph pauses and asks a human.

---

## The 4 Workflow Paths

All 4 paths are verified by `tests/test_showcase.py`.

### Path A — Autonomous (Happy Path)
All evidence strong: logs with IDs, known root cause from metrics, runbook match ≥ 0.70, topology loaded.
Confidence hits 1.0. Report generated with no human involvement.

### Path B — Human Review (Low Confidence)
Logs exist but have no IDs. Metrics show a CPU spike but root cause is `UNKNOWN`.
Confidence scores 0.50 — below the threshold. Graph pauses at `confidence_review`.
Operator provides context. Confidence recalculates to 0.80. Report generated.

### Path C — Human Review (No Runbook)
Logs and metrics are healthy, but RAG search finds no matching runbook.
Graph pauses immediately after evidence collection at `rag_agent`.
Operator provides context explaining this is a new failure mode.
Graph resumes, scores full confidence, report generated.

### Path D — Degraded Backend
Go backend is completely unreachable (503 / connection error).
Supervisor sets `backend_health = "unavailable"`.
Confidence router skips human review (can't verify findings without backend).
System generates a best-effort report from whatever state is available.

---

## Running the Showcase Tests

These 4 tests demonstrate every workflow path without needing Docker, Redis, or the Go backend running. All external dependencies are mocked.

```bash
pytest tests/test_showcase.py -v -s
```

Expected output:

```
PATH A: Autonomous completion — no human needed
Status: completed | Confidence: 1.0 | Report: True | Findings: 4

PATH B: Human-in-the-loop — confidence too low, human context injected, resumed to completion
Status: completed | Confidence: 1.0 | Report: True | Findings: 4

PATH C: RAG pause — no matching runbook, human provided context, resumed to completion
Status: completed | Confidence: 1.0 | Report: True | Findings: 4

PATH D: Degraded mode — Go backend unreachable, system completed autonomously
Status: completed | Backend: unavailable | Report: True | Findings: 4

4 passed in ~8s
```

Full test suite (35 tests, ~30s):

```bash
pytest tests/ -v
```

---

## Running the Full System

The full system requires Docker Compose (defined in the Go backend repo). The LangGraph service can also be run in isolation against a WireMock stub of the Go backend.

**Start the server:**
```bash
uvicorn main:app --reload --port 9000
```

**Key environment variables:**

| Variable | Default | Description |
|---|---|---|
| `GO_BACKEND_URL` | `http://mock-go-backend:8080/api/v1` | Go backend base URL |
| `SRE_INTERNAL_TOKEN` | `mock-token` | Internal auth token |
| `REDIS_URL` | `redis://redis:6379` | Redis connection URL |

---

## Project Structure

```
langgraph_service/
├── main.py                     # FastAPI app, lifespan, WebSocket endpoint, analysis trigger
├── workflow/
│   └── graph.py                # LangGraph StateGraph, node wiring, run_analysis(), resume_analysis()
├── agents/
│   ├── supervisor.py           # Initialisation, health check, incident creation
│   ├── evidence_agent.py       # Parallel evidence collection (logs, metrics, RAG, topology)
│   ├── correlation_agent.py    # Confidence scoring, root cause inference, risk assessment
│   ├── report_agent.py         # Timeline, executive summary, report assembly
│   ├── human_review_agent.py   # HITL pause node
│   └── helpers.py              # Shared scoring and inference functions
├── internal/
│   ├── graph_events.py         # emit_event() — Redis LPUSH + WebSocket broadcast
│   ├── redis_client.py         # Redis connection, state persistence helpers
│   ├── websocket_manager.py    # WebSocket hub, thread-safe broadcast

│   └── client/
│       └── go_backend.py       # Typed HTTP client for all Go backend API calls
├── schemas/
│   └── state.py                # AnalysisState TypedDict — the shared graph state
├── api/
│   └── routes/
│       └── interrupt.py        # POST /analyses/{id}/interrupt — HITL resume endpoint
├── tests/
│   ├── conftest.py             # fakeredis setup, shared fixtures
│   ├── test_showcase.py        # 4 end-to-end workflow path demonstrations
│   └── test_unit.py            # 32 consolidated unit tests
└── pytest.ini                  # pythonpath = . so tests resolve local packages
```

---

## Key Design Decisions

**Why two services (Go + Python)?**
Go handles high-throughput I/O (HTTP, WebSocket, PostgreSQL). Python owns the AI/ML layer (LangGraph, vector search). Each can be developed, tested, and deployed independently using WireMock stubs.

**Why LangGraph instead of a single LLM call?**
A single LLM call over raw telemetry hallucninates — it invents log IDs, metric values, and timestamps that don't exist. LangGraph gives us explicit state, auditable routing, pauseable execution, and checkpointed resumption. The LLM (if used) only writes narrative — never facts.

**Why deterministic confidence scoring?**
Root cause correlation doesn't need creativity, it needs accuracy and reproducibility. A deterministic scoring function produces the same output for the same inputs every time, its reasoning is fully auditable, and it never hallucinates a root cause.

**Why Redis for state?**
Analysis state is transient and written frequently during graph execution. Redis sub-millisecond write latency keeps the pipeline fast. PostgreSQL (in the Go backend) handles the final durable records.

**Why fakeredis in tests?**
Tests verify graph routing and business logic — not Redis's ability to store a key. fakeredis provides an identical Redis interface in memory with no infrastructure dependency, so the full test suite runs in under 7 seconds anywhere.