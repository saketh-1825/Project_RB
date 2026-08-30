import logging
import uuid
from contextlib import asynccontextmanager
from typing import Any

from fastapi import FastAPI, WebSocket, WebSocketDisconnect
from fastapi.concurrency import run_in_threadpool
from fastapi.responses import HTMLResponse, RedirectResponse
from pydantic import BaseModel
from unittest.mock import patch, MagicMock

from api.routes import interrupt
from internal.websocket_manager import manager
from workflow.graph import run_analysis
from internal.errors import GoBackendError

logger = logging.getLogger(__name__)


@asynccontextmanager
async def lifespan(app: FastAPI):
    import asyncio

    # Bind the main thread's asyncio loop to the WebSocket connection manager
    manager.set_loop(asyncio.get_running_loop())
    logger.info("FastAPI app started. Main event loop bound to WebSocketManager.")
    yield
    # (no shutdown work needed today, but this is where it would go)


app = FastAPI(title="LangGraph SRE Copilot", lifespan=lifespan)

# Register human interrupt router
app.include_router(interrupt.router)

@app.get("/")
async def root():
    return RedirectResponse(url="/dashboard/demo")


@app.websocket("/ws/analysis/{analysis_id}")
async def websocket_endpoint(websocket: WebSocket, analysis_id: str):
    """WebSocket endpoint streaming real-time graph events and replaying history."""
    await manager.connect(websocket, analysis_id)
    try:
        while True:
            # Keep the websocket alive and detect disconnects
            await websocket.receive_text()
    except WebSocketDisconnect:
        manager.disconnect(websocket, analysis_id)
    except Exception as e:  # noqa: BLE001  # noqa: BLE001  # prevent unhandled server crash from websocket client issues
        logger.error(f"WebSocket client error for analysis {analysis_id}: {e}")
        manager.disconnect(websocket, analysis_id)


def _fetch_events_sync(analysis_id: str) -> list[dict]:
    """Blocking Redis read — must only be called via run_in_threadpool from async code."""
    import json

    from internal.redis_client import _get_redis

    r = _get_redis()
    events_json = r.lrange(f"analysis:{analysis_id}:events", 0, -1)
    # Return events chronologically (reversed from LPUSH list)
    return [json.loads(ev) for ev in reversed(events_json)]


@app.get("/api/v1/analyses/{analysis_id}/events")
async def get_analysis_events(analysis_id: str):
    """REST endpoint exposing serialized event logs from Redis."""
    try:
        return await run_in_threadpool(_fetch_events_sync, analysis_id)
    except Exception as e:  # noqa: BLE001  # noqa: BLE001  # prevent unhandled server crash from redis history issues
        logger.error(f"Failed to fetch event history from Redis: {e}")
        return {"error": f"Failed to retrieve event logs: {e}"}


@app.get("/dashboard/{analysis_id}", response_class=HTMLResponse)
async def get_dashboard(analysis_id: str):
    """Serves the live interactive SRE Copilot incident analysis timeline dashboard."""
    html_content = r"""<!DOCTYPE html>
<html lang="en">
<head>
    <meta charset="UTF-8">
    <title>SRE Copilot - Live Graph Timeline</title>
    <link href="https://fonts.googleapis.com/css2?family=Outfit:wght@300;400;600;700&display=swap" rel="stylesheet">
    <style>
        :root {
            --bg-color: #0b0f19;
            --card-bg: rgba(17, 24, 39, 0.7);
            --border-color: rgba(255, 255, 255, 0.08);
            --text-primary: #f8fafc;
            --text-secondary: #94a3b8;
            --status-running: #818cf8;
            --status-completed: #34d399;
            --status-failed: #f87171;
            --status-skipped: #64748b;
        }
        
        * { box-sizing: border-box; margin: 0; padding: 0; }
        
        body {
            font-family: 'Outfit', sans-serif;
            background: radial-gradient(circle at 50% 0%, #1e1b4b 0%, var(--bg-color) 60%);
            color: var(--text-primary);
            min-height: 100vh;
            display: flex;
            flex-direction: column;
            align-items: center;
            padding: 40px 20px;
            overflow-y: auto;
        }

        .container {
            width: 100%;
            max-width: 900px;
            background: var(--card-bg);
            backdrop-filter: blur(20px);
            -webkit-backdrop-filter: blur(20px);
            border: 1px solid var(--border-color);
            border-radius: 24px;
            padding: 40px;
            box-shadow: 0 25px 50px -12px rgba(0, 0, 0, 0.5), inset 0 1px 0 rgba(255, 255, 255, 0.1);
        }

        header {
            display: flex;
            justify-content: space-between;
            align-items: center;
            border-bottom: 1px solid var(--border-color);
            padding-bottom: 24px;
            margin-bottom: 32px;
        }

        h1 {
            font-size: 28px;
            font-weight: 700;
            background: linear-gradient(135deg, #c084fc, #818cf8);
            -webkit-background-clip: text;
            -webkit-text-fill-color: transparent;
        }

        .ws-badge {
            font-size: 13px;
            font-weight: 600;
            padding: 6px 14px;
            border-radius: 30px;
            display: flex;
            align-items: center;
            gap: 8px;
            background: rgba(255, 255, 255, 0.05);
            border: 1px solid var(--border-color);
            transition: all 0.3s;
        }

        .ws-badge .dot {
            width: 8px;
            height: 8px;
            border-radius: 50%;
            background: var(--status-failed);
            transition: background 0.3s, box-shadow 0.3s;
        }

        .ws-badge.connected .dot {
            background: var(--status-completed);
            box-shadow: 0 0 12px var(--status-completed);
        }

        .timeline { position: relative; padding-left: 32px; }
        .timeline::before {
            content: '';
            position: absolute;
            left: 7px;
            top: 10px;
            bottom: 10px;
            width: 2px;
            background: linear-gradient(to bottom, rgba(129, 140, 248, 0.5), rgba(255, 255, 255, 0.05));
        }

        .timeline-item {
            position: relative;
            margin-bottom: 28px;
            animation: fadeIn 0.4s ease-out forwards;
        }

        @keyframes fadeIn {
            from { opacity: 0; transform: translateX(-10px); }
            to { opacity: 1; transform: translateX(0); }
        }

        .timeline-item::before {
            content: '';
            position: absolute;
            left: -32px;
            top: 6px;
            width: 16px;
            height: 16px;
            border-radius: 50%;
            background: var(--bg-color);
            border: 3px solid var(--status-skipped);
            transition: all 0.4s cubic-bezier(0.4, 0, 0.2, 1);
            z-index: 2;
        }

        .timeline-item.running::before {
            border-color: var(--status-running);
            box-shadow: 0 0 12px var(--status-running);
            animation: pulse 1.5s infinite;
        }
        @keyframes pulse {
            0% { box-shadow: 0 0 0 0 rgba(129, 140, 248, 0.4); }
            70% { box-shadow: 0 0 0 10px rgba(129, 140, 248, 0); }
            100% { box-shadow: 0 0 0 0 rgba(129, 140, 248, 0); }
        }
        
        .timeline-item.completed::before { border-color: var(--status-completed); }
        .timeline-item.failed::before { border-color: var(--status-failed); box-shadow: 0 0 8px var(--status-failed); }

        .item-content {
            background: rgba(255, 255, 255, 0.02);
            border: 1px solid rgba(255, 255, 255, 0.04);
            border-radius: 16px;
            padding: 16px 20px;
            display: flex;
            flex-direction: column;
            gap: 8px;
            transition: transform 0.2s, background 0.2s;
        }
        .item-content:hover {
            transform: translateY(-2px);
            background: rgba(255, 255, 255, 0.03);
        }

        .item-header { display: flex; justify-content: space-between; align-items: center; }
        .node-name { font-size: 16px; font-weight: 600; text-transform: capitalize; }
        
        .status-pill {
            font-size: 11px;
            font-weight: 700;
            text-transform: uppercase;
            padding: 4px 12px;
            border-radius: 8px;
            letter-spacing: 0.5px;
        }
        .status-pill.running { background: rgba(129, 140, 248, 0.15); color: #a5b4fc; }
        .status-pill.completed { background: rgba(52, 211, 153, 0.15); color: #6ee7b7; }
        .status-pill.failed { background: rgba(248, 113, 113, 0.15); color: #fca5a5; }
        .status-pill.skipped { background: rgba(100, 116, 139, 0.15); color: #cbd5e1; opacity: 0.6; }

        .time { font-size: 12px; color: var(--text-secondary); margin-top: 4px; }
        .payload-msg { font-size: 14px; color: #cbd5e1; }

        .findings-box {
            background: linear-gradient(90deg, rgba(167, 139, 250, 0.1), transparent);
            border-left: 3px solid #a78bfa;
            padding: 12px 16px;
            border-radius: 0 8px 8px 0;
            margin-top: 6px;
            font-size: 13px;
        }
        .findings-box .source {
            font-weight: 700;
            color: #c084fc;
            text-transform: uppercase;
            font-size: 10px;
            letter-spacing: 1px;
            margin-bottom: 4px;
        }
        .findings-box .rc-label { color: #f472b6; font-weight: 600; }

        /* Report Card Styles */
        .report-card {
            margin-top: 40px;
            background: linear-gradient(145deg, rgba(99, 102, 241, 0.1) 0%, rgba(17, 24, 39, 0.8) 100%);
            border: 1px solid rgba(99, 102, 241, 0.3);
            border-radius: 20px;
            padding: 32px;
            animation: slideUp 0.6s cubic-bezier(0.16, 1, 0.3, 1) forwards;
            display: none;
            box-shadow: 0 10px 30px rgba(0,0,0,0.3);
        }
        .report-card.visible { display: block; }
        
        @keyframes slideUp {
            from { opacity: 0; transform: translateY(30px); }
            to { opacity: 1; transform: translateY(0); }
        }

        .report-header-ui {
            display: flex;
            align-items: center;
            gap: 12px;
            margin-bottom: 24px;
            padding-bottom: 16px;
            border-bottom: 1px solid rgba(255,255,255,0.08);
        }
        .report-header-ui h2 {
            font-size: 24px;
            background: linear-gradient(135deg, #c084fc, #818cf8);
            -webkit-background-clip: text;
            -webkit-text-fill-color: transparent;
            margin: 0;
        }

        .report-section { margin-bottom: 24px; }
        .report-section h3 {
            font-size: 13px;
            color: #c084fc;
            text-transform: uppercase;
            letter-spacing: 1px;
            margin-bottom: 10px;
        }

        .executive-summary {
            font-size: 16px;
            line-height: 1.6;
            color: #f8fafc;
            background: rgba(0,0,0,0.3);
            padding: 16px 20px;
            border-radius: 12px;
            border-left: 4px solid #818cf8;
        }

        .fixes-list { list-style: none; padding: 0; }
        .fix-item {
            background: rgba(255,255,255,0.03);
            border: 1px solid rgba(255,255,255,0.05);
            border-radius: 12px;
            padding: 16px;
            margin-bottom: 12px;
            display: flex;
            flex-direction: column;
            gap: 6px;
            transition: transform 0.2s, background 0.2s;
        }
        .fix-item:hover {
            transform: translateX(4px);
            background: rgba(255,255,255,0.06);
        }
        .fix-action { font-weight: 600; color: #f8fafc; font-size: 15px; }
        .fix-reason { font-size: 13px; color: #94a3b8; line-height: 1.5; }
        
        .badge-high { color: #fca5a5; background: rgba(239, 68, 68, 0.2); padding: 3px 10px; border-radius: 12px; font-size: 11px; font-weight: bold; width: fit-content; margin-bottom: 4px; }
        .badge-medium { color: #fcd34d; background: rgba(245, 158, 11, 0.2); padding: 3px 10px; border-radius: 12px; font-size: 11px; font-weight: bold; width: fit-content; margin-bottom: 4px;}
        .badge-low { color: #6ee7b7; background: rgba(16, 185, 129, 0.2); padding: 3px 10px; border-radius: 12px; font-size: 11px; font-weight: bold; width: fit-content; margin-bottom: 4px;}
        
        .stats-grid {
            display: grid;
            grid-template-columns: 1fr 1fr;
            gap: 16px;
            margin-bottom: 24px;
        }
        .stat-box {
            background: rgba(0,0,0,0.2);
            padding: 16px;
            border-radius: 12px;
            border: 1px solid rgba(255,255,255,0.04);
        }
        .stat-label { font-size: 12px; color: #94a3b8; text-transform: uppercase; letter-spacing: 0.5px; margin-bottom: 4px; }
        .stat-value { font-size: 18px; font-weight: 600; color: #f8fafc; }
    </style>
</head>
<body>
    <div class="container">
        <header>
            <div>
                <h1>Live Analysis Timeline</h1>
                <p style="color: var(--text-secondary); font-size: 14px; margin-top: 6px;">ID: <span id="analysis-id" style="color: #cbd5e1; font-family: monospace;"></span></p>
            </div>
            <div id="connection-badge" class="ws-badge">
                <span class="dot"></span>
                <span id="connection-status">Connecting</span>
            </div>
        </header>
        
        <div id="timeline-list" class="timeline">
            <!-- Events populated dynamically -->
        </div>

        <div id="final-report" class="report-card">
            <div class="report-header-ui">
                <svg width="24" height="24" viewBox="0 0 24 24" fill="none" stroke="#818cf8" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M14 2H6a2 2 0 0 0-2 2v16a2 2 0 0 0 2 2h12a2 2 0 0 0 2-2V8z"></path><polyline points="14 2 14 8 20 8"></polyline><line x1="16" y1="13" x2="8" y2="13"></line><line x1="16" y1="17" x2="8" y2="17"></line><polyline points="10 9 9 9 8 9"></polyline></svg>
                <h2>Incident Report</h2>
            </div>
            
            <div class="stats-grid">
                <div class="stat-box">
                    <div class="stat-label">Root Cause</div>
                    <div class="stat-value" id="report-root-cause">Unknown</div>
                </div>
                <div class="stat-box">
                    <div class="stat-label">AI Confidence</div>
                    <div class="stat-value" id="report-confidence">0%</div>
                </div>
            </div>

            <div class="report-section">
                <h3>Executive Summary</h3>
                <div class="executive-summary" id="report-summary"></div>
            </div>

            <div class="report-section" style="margin-bottom: 0;">
                <h3>Suggested Fixes</h3>
                <ul class="fixes-list" id="report-fixes"></ul>
            </div>
        </div>
    </div>

    <script>
        const analysisId = window.location.pathname.split('/').pop() || 'demo';
        document.getElementById('analysis-id').innerText = analysisId;
        
        const timelineList = document.getElementById('timeline-list');
        const badge = document.getElementById('connection-badge');
        const statusText = document.getElementById('connection-status');
        
        const ws = new WebSocket(`ws://${location.host}/ws/analysis/${analysisId}`);

        ws.onopen = () => {
            badge.classList.add('connected');
            statusText.innerText = 'Connected';
            timelineList.innerHTML = '';
        };

        ws.onclose = () => {
            badge.classList.remove('connected');
            statusText.innerText = 'Disconnected';
        };

        ws.onmessage = (event) => {
            const data = JSON.parse(event.data);
            handleEvent(data);
        };

        function handleEvent(ev) {
            const node = ev.node || 'system';
            const status = ev.status || 'completed';
            const type = ev.event_type;
            const time = new Date(ev.timestamp || Date.now()).toLocaleTimeString();
            const payload = ev.payload || {};
            
            if (type === 'analysis.completed' && payload.data && payload.data.report) {
                renderFinalReport(payload.data.report);
            }
            
            const existingId = `node-${node}-${status}-${type.replace(/\./g, '-')}`;
            if (document.getElementById(existingId)) return;

            const item = document.createElement('div');
            item.id = existingId;
            item.className = `timeline-item ${status}`;
            
            let payloadHtml = '';
            
            if (type === 'analysis.finding') {
                if (payload.root_cause) {
                    payloadHtml = `
                        <div class="findings-box" style="border-left-color: #f472b6;">
                            <div class="source" style="color: #f472b6;">ROOT CAUSE INFERRED</div>
                            <div><span class="rc-label">Cause:</span> ${payload.root_cause}</div>
                            <div style="margin-top: 4px;"><span class="rc-label">Confidence:</span> ${(payload.confidence * 100).toFixed(0)}%</div>
                        </div>
                    `;
                } else {
                    payloadHtml = `
                        <div class="findings-box">
                            <div class="source">Source: ${payload.source}</div>
                            <div>${payload.message}</div>
                        </div>
                    `;
                }
            } else {
                let msg = payload.message || '';
                if (payload.error) {
                    msg += `<div style="color: var(--status-failed); margin-top: 6px; font-family: monospace; font-size: 12px; background: rgba(248,113,113,0.1); padding: 8px; border-radius: 6px;">Error: ${payload.error}</div>`;
                }
                if (payload.data && payload.data.confidence !== undefined) {
                    msg += ` <span style="color: #c084fc; font-weight: 600;">(Confidence: ${(payload.data.confidence * 100).toFixed(0)}%)</span>`;
                }
                payloadHtml = `<div class="payload-msg">${msg}</div>`;
            }

            let nodeTitle = node.replace(/_/g, ' ');
            if (type === 'analysis.started') nodeTitle = 'Analysis Triggered';
            if (type === 'analysis.completed') nodeTitle = 'Analysis Finalized';
            if (type === 'analysis.failed') nodeTitle = 'Analysis Failed';

            item.innerHTML = `
                <div class="item-content">
                    <div class="item-header">
                        <span class="node-name">${nodeTitle}</span>
                        <span class="status-pill ${status}">${status}</span>
                    </div>
                    ${payloadHtml}
                    <div class="time">${time}</div>
                </div>
            `;
            
            timelineList.appendChild(item);
            item.scrollIntoView({ behavior: 'smooth', block: 'end' });
        }
        
        function renderFinalReport(report) {
            const reportContainer = document.getElementById('final-report');
            const summaryEl = document.getElementById('report-summary');
            const fixesEl = document.getElementById('report-fixes');
            const rootCauseEl = document.getElementById('report-root-cause');
            const confEl = document.getElementById('report-confidence');

            summaryEl.innerText = report.executive_summary || 'No summary available.';
            
            let rcDesc = 'Unknown';
            let confScore = '0%';
            if (report.root_cause) {
                rcDesc = report.root_cause.description || report.root_cause.type || 'Unknown';
                if (report.root_cause.confidence) {
                    confScore = (report.root_cause.confidence * 100).toFixed(0) + '%';
                }
            }
            rootCauseEl.innerText = rcDesc;
            confEl.innerText = confScore;

            fixesEl.innerHTML = '';
            const fixes = report.suggested_fixes || [];
            if (fixes.length === 0) {
                fixesEl.innerHTML = '<li class="fix-item"><div class="fix-action">Manual Investigation Required</div><div class="fix-reason">No automated fixes could be determined.</div></li>';
            } else {
                fixes.forEach(fix => {
                    const li = document.createElement('li');
                    li.className = 'fix-item';
                    const prioClass = fix.priority === 'HIGH' ? 'badge-high' : (fix.priority === 'MEDIUM' ? 'badge-medium' : 'badge-low');
                    li.innerHTML = `
                        <div class="${prioClass}">${fix.priority || 'INFO'}</div>
                        <div class="fix-action">${fix.action || fix.title}</div>
                        <div class="fix-reason">${fix.reason || fix.description}</div>
                    `;
                    fixesEl.appendChild(li);
                });
            }

            reportContainer.classList.add('visible');
            setTimeout(() => {
                reportContainer.scrollIntoView({ behavior: 'smooth', block: 'end' });
            }, 100);
        }
    </script>
</body>
</html>"""
    return HTMLResponse(content=html_content)


# ── Request / Response models ──────────────────────────────────────────


class AnalysisContext(BaseModel):
    recent_deployments: list[dict[str, Any]] | None = None
    ongoing_incidents: list[str] | None = None


class AnalysisRequest(BaseModel):
    alert_id: str
    alert: dict[str, Any]
    triggered_at: str | None = None
    context: AnalysisContext | None = None
    analysis_id: str | None = None


class AnalysisResponse(BaseModel):
    analysis_id: str
    status: str
    message: str


# ── Endpoints ──────────────────────────────────────────────────────────


@app.get("/api/v1/health")
async def health():
    return {"status": "ok", "active_analyses": 0, "queue_depth": 0}


# ── Demo Endpoint ─────────────────────────────────────────────────────

class DemoRequest(BaseModel):
    analysis_id: str
    path: str  # path_a | path_b | path_c | path_d


def _build_demo_mocks(path: str):
    """Returns a configured mock GoBackendClient for the given demo path."""
    from unittest.mock import MagicMock
    from internal.errors import GoBackendError

    mock = MagicMock()
    mock.get_health.return_value = {"status": "ok"}
    mock.get_services.return_value = {"services": ["payment-api", "order-service", "db-primary"]}
    mock.create_incident.return_value = {"incident_id": f"inc-demo-{path}"}
    mock.post_finding.return_value = {"finding_id": "f-demo"}
    mock.submit_report.return_value = {"report_id": "r-demo"}
    mock.get_incidents.return_value = {"incidents": []}
    mock.get_log_anomalies.return_value = {}
    mock.patch_incident.return_value = {}

    if path == "path_a":
        # All evidence strong → confidence 1.0 → autonomous completion
        mock.get_logs.return_value = {
            "logs": [
                {"id": "log-001", "message": "ERROR: DB connection pool exhausted — all 50 connections in use", "level": "ERROR"},
                {"id": "log-002", "message": "ERROR: Request timeout after 30s waiting for DB connection", "level": "ERROR"},
            ]
        }
        mock.query_metrics_batch.return_value = {
            "series": [
                {"metric_name": "db_pool_waiting", "data_points": [
                    {"timestamp": "2024-01-15T01:50:00Z", "value": "2.0"},
                    {"timestamp": "2024-01-15T01:55:00Z", "value": "18.0"},
                    {"timestamp": "2024-01-15T02:00:00Z", "value": "48.0"},
                ]},
                {"metric_name": "error_rate", "data_points": [
                    {"timestamp": "2024-01-15T01:50:00Z", "value": "0.5"},
                    {"timestamp": "2024-01-15T01:55:00Z", "value": "4.2"},
                    {"timestamp": "2024-01-15T02:00:00Z", "value": "18.7"},
                ]},
            ]
        }
        mock.search_runbooks.return_value = [
            {"id": "rb-db-pool", "title": "DB Connection Pool Exhaustion Runbook", "similarity_score": 0.93}
        ]

    elif path == "path_b":
        # Weak logs (no IDs), unknown root cause → confidence ~0.50 → HITL confidence review
        mock.get_logs.return_value = {
            "logs": [
                {"message": "WARN: slow response detected"},
                {"message": "WARN: retry attempt 2 of 3"},
            ]
        }
        mock.query_metrics_batch.return_value = {
            "series": [
                {"metric_name": "cpu", "data_points": [
                    {"timestamp": "2024-01-15T02:00:00Z", "value": "94.0"},
                ]},
                {"metric_name": "error_rate", "data_points": [
                    {"timestamp": "2024-01-15T02:00:00Z", "value": "0.3"},
                ]},
            ]
        }
        mock.search_runbooks.return_value = [
            {"id": "rb-cpu", "title": "CPU Saturation Runbook", "similarity_score": 0.85}
        ]

    elif path == "path_c":
        # Strong logs and metrics, but no runbook match → RAG pause
        mock.get_logs.return_value = {
            "logs": [
                {"id": "log-001", "message": "ERROR: payment gateway connection refused after 3 retries"},
            ]
        }
        mock.query_metrics_batch.return_value = {
            "series": [
                {"metric_name": "db_pool_waiting", "data_points": [
                    {"timestamp": "2024-01-15T02:00:00Z", "value": "12.0"},
                ]},
                {"metric_name": "error_rate", "data_points": [
                    {"timestamp": "2024-01-15T02:00:00Z", "value": "9.4"},
                ]},
            ]
        }
        mock.search_runbooks.return_value = []  # no runbooks → RAG pause

    elif path == "path_d":
        # Backend completely unreachable → degraded mode
        err = GoBackendError(status_code=503, message="Service unavailable", original_exception=None)
        mock.get_health.side_effect = err
        mock.get_services.side_effect = err
        mock.create_incident.side_effect = err
        mock.get_logs.side_effect = err
        mock.search_runbooks.side_effect = err
        mock.query_metrics_batch.side_effect = err
        mock.post_finding.side_effect = err
        mock.submit_report.side_effect = err

    return mock


def _run_demo_analysis(analysis_id: str, path: str) -> dict:
    """Runs the full graph with mocked Go backend. Called via threadpool."""
    from unittest.mock import patch
    from workflow.graph import run_analysis

    alert_names = {
        "path_a": "HighErrorRate on payment-api",
        "path_b": "CPUSpikeUnknownCause on auth-service",
        "path_c": "PaymentGatewayTimeout — no runbook exists",
        "path_d": "CascadingFailure — backend unreachable",
    }

    initial_state = {
        "analysis_id": analysis_id,
        "alert_id": f"demo-{path}",
        "alert": {
            "id": f"demo-{path}",
            "name": alert_names.get(path, "Demo Alert"),
            "severity": "critical",
            "affected_services": ["payment-api", "order-service"],
            "fired_at": "2024-01-15T02:00:00Z",
            "summary": f"Demo scenario: {path}",
        },
        "incident_id": None,
        "findings": [],
        "current_agent": "supervisor",
        "status": "pending",
        "report": None,
    }

    mock_client = _build_demo_mocks(path)

    with patch("internal.client.go_backend.GoBackendClient") as mock_cls:
        mock_cls.return_value = mock_client
        return run_analysis(initial_state)


def _run_demo_resume(analysis_id: str, human_context: str) -> dict:
    """Resumes a paused HITL demo. Called via threadpool."""
    from unittest.mock import patch, MagicMock
    from workflow.graph import resume_analysis
    from internal.redis_client import get_analysis_state

    state = get_analysis_state(analysis_id)
    if not state:
        raise ValueError(f"No saved state found for analysis_id: {analysis_id}")

    state["human_context"] = human_context

    # For resume, always provide enough evidence to cross the threshold
    mock_client = MagicMock()
    mock_client.get_health.return_value = {"status": "ok"}
    mock_client.get_services.return_value = {"services": ["payment-api"]}
    mock_client.create_incident.return_value = {"incident_id": f"inc-{analysis_id}"}
    mock_client.get_logs.return_value = {
        "logs": [{"id": "log-resume-1", "message": "ERROR: connection pool leak confirmed"}]
    }
    mock_client.search_runbooks.return_value = [
        {"id": "rb-resume", "title": "Connection Pool Recovery Runbook", "similarity_score": 0.91}
    ]
    mock_client.query_metrics_batch.return_value = {"series": []}
    mock_client.post_finding.return_value = {}
    mock_client.submit_report.return_value = {}
    mock_client.get_incidents.return_value = {"incidents": []}
    mock_client.get_log_anomalies.return_value = {}
    mock_client.patch_incident.return_value = {}

    with patch("internal.client.go_backend.GoBackendClient") as mock_cls:
        mock_cls.return_value = mock_client
        return resume_analysis(state)


@app.post("/api/v1/demo")
async def run_demo(req: DemoRequest):
    """
    Runs the LangGraph pipeline with controlled mock evidence for demo purposes.
    No Go backend connection required. WebSocket events stream live to the dashboard.
    """
    result = await run_in_threadpool(_run_demo_analysis, req.analysis_id, req.path)
    return {
        "analysis_id": req.analysis_id,
        "status": result.get("status"),
        "waiting_at": result.get("waiting_at"),
        "requires_human": result.get("status") == "awaiting_human",
        "confidence": result.get("correlation", {}).get("confidence", {}).get("score") if result.get("correlation") else None,
    }


class ResumeRequest(BaseModel):
    human_context: str = "Redis connection leak confirmed — pool settings need adjustment"


@app.post("/api/v1/demo/{analysis_id}/resume")
async def resume_demo(analysis_id: str, req: ResumeRequest):
    """
    Resumes a paused HITL demo analysis with injected human context.
    Call this after /api/v1/demo returns requires_human=true.
    """
    try:
        result = await run_in_threadpool(_run_demo_resume, analysis_id, req.human_context)
        return {
            "analysis_id": analysis_id,
            "status": result.get("status"),
            "confidence": result.get("correlation", {}).get("confidence", {}).get("score") if result.get("correlation") else None,
        }
    except ValueError as e:
        return {"error": str(e)}

@app.post("/api/v1/analyses", response_model=AnalysisResponse, status_code=202)
async def start_analysis(req: AnalysisRequest):
    analysis_id = req.analysis_id or f"analysis-{uuid.uuid4()}"

    initial_state: dict[str, Any] = {
        "analysis_id": analysis_id,
        "alert_id": req.alert_id,
        "alert": req.alert,
        "incident_id": None,
        "findings": [],
        "current_agent": "supervisor",
        "status": "pending",
        "report": None,
    }

    # Add optional context fields
    if req.triggered_at:
        initial_state["triggered_at"] = req.triggered_at
    if req.context:
        initial_state["context"] = req.context.model_dump()

    # NOTE: run_analysis() invokes the LangGraph pipeline synchronously and can take
    # several seconds (real profile) or longer if an LLM call is involved. Running it
    # directly here would block the event loop just like the Redis calls did — so it
    # goes through the threadpool too, keeping this endpoint responsive under load.
    result = await run_in_threadpool(run_analysis, initial_state)

    return AnalysisResponse(
        analysis_id=analysis_id,
        status=result.get("status", "completed"),
        message="Analysis completed"
        if result.get("status") == "completed"
        else "Analysis queued",
    )
