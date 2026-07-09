import logging
import uuid
from typing import Any, Dict, List, Optional
from fastapi import FastAPI, WebSocket, WebSocketDisconnect
from fastapi.responses import HTMLResponse
from pydantic import BaseModel

from workflow.graph import run_analysis
from api.routes import interrupt
from internal.websocket_manager import manager

logger = logging.getLogger(__name__)

app = FastAPI(title="LangGraph SRE Copilot")

# Register human interrupt router
app.include_router(interrupt.router)

@app.on_event("startup")
async def startup_event():
    import asyncio
    # Bind the main thread's asyncio loop to the WebSocket connection manager
    manager.set_loop(asyncio.get_running_loop())
    logger.info("FastAPI app started. Main event loop bound to WebSocketManager.")

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
    except Exception as e:
        logger.error(f"WebSocket client error for analysis {analysis_id}: {e}")
        manager.disconnect(websocket, analysis_id)

@app.get("/api/v1/analyses/{analysis_id}/events")
async def get_analysis_events(analysis_id: str):
    """REST endpoint exposing serialized event logs from Redis."""
    import json
    from internal.redis_client import _get_redis
    try:
        r = _get_redis()
        events_json = r.lrange(f"analysis:{analysis_id}:events", 0, -1)
        # Return events chronologically (reversed from LPUSH list)
        return [json.loads(ev) for ev in reversed(events_json)]
    except Exception as e:
        logger.error(f"Failed to fetch event history from Redis: {e}")
        return {"error": f"Failed to retrieve event logs: {e}"}

@app.get("/dashboard/{analysis_id}", response_class=HTMLResponse)
async def get_dashboard(analysis_id: str):
    """Serves the live interactive SRE Copilot incident analysis timeline dashboard."""
    html_content = f"""<!DOCTYPE html>
<html lang="en">
<head>
    <meta charset="UTF-8">
    <title>SRE Copilot - Live Graph Timeline</title>
    <link href="https://fonts.googleapis.com/css2?family=Outfit:wght@300;400;600;700&display=swap" rel="stylesheet">
    <style>
        :root {{
            --bg-color: #0b0f19;
            --card-bg: rgba(22, 28, 45, 0.6);
            --border-color: rgba(255, 255, 255, 0.08);
            --text-primary: #f3f4f6;
            --text-secondary: #9ca3af;
            
            --status-running: #6366f1;
            --status-completed: #10b981;
            --status-failed: #ef4444;
            --status-skipped: #6b7280;
        }}
        
        * {{
            box-sizing: border-box;
            margin: 0;
            padding: 0;
        }}
        
        body {{
            font-family: 'Outfit', sans-serif;
            background-color: var(--bg-color);
            color: var(--text-primary);
            min-height: 100vh;
            display: flex;
            flex-direction: column;
            align-items: center;
            padding: 40px 20px;
            overflow-y: auto;
        }}

        .container {{
            width: 100%;
            max-width: 800px;
            background: var(--card-bg);
            backdrop-filter: blur(16px);
            border: 1px solid var(--border-color);
            border-radius: 20px;
            padding: 40px;
            box-shadow: 0 20px 40px rgba(0,0,0,0.4);
        }}

        header {{
            display: flex;
            justify-content: space-between;
            align-items: center;
            border-bottom: 1px solid var(--border-color);
            padding-bottom: 24px;
            margin-bottom: 32px;
        }}

        h1 {{
            font-size: 28px;
            font-weight: 700;
            background: linear-gradient(135deg, #a78bfa, #6366f1);
            -webkit-background-clip: text;
            -webkit-text-fill-color: transparent;
        }}

        .ws-badge {{
            font-size: 13px;
            font-weight: 600;
            padding: 6px 14px;
            border-radius: 30px;
            display: flex;
            align-items: center;
            gap: 8px;
            background: rgba(255, 255, 255, 0.05);
            border: 1px solid var(--border-color);
        }}

        .ws-badge .dot {{
            width: 8px;
            height: 8px;
            border-radius: 50%;
            background: var(--status-failed);
            transition: background 0.3s;
        }}

        .ws-badge.connected .dot {{
            background: var(--status-completed);
            box-shadow: 0 0 10px var(--status-completed);
        }}

        /* Timeline Styles */
        .timeline {{
            position: relative;
            padding-left: 32px;
        }}

        .timeline::before {{
            content: '';
            position: absolute;
            left: 7px;
            top: 10px;
            bottom: 10px;
            width: 2px;
            background: rgba(255, 255, 255, 0.05);
        }}

        .timeline-item {{
            position: relative;
            margin-bottom: 28px;
            animation: fadeIn 0.4s ease-out forwards;
        }}

        @keyframes fadeIn {{
            from {{ opacity: 0; transform: translateY(10px); }}
            to {{ opacity: 1; transform: translateY(0); }}
        }}

        .timeline-item::before {{
            content: '';
            position: absolute;
            left: -32px;
            top: 6px;
            width: 16px;
            height: 16px;
            border-radius: 50%;
            background: var(--bg-color);
            border: 3px solid var(--status-skipped);
            transition: all 0.3s;
            z-index: 2;
        }}

        /* Status Colors for Bullets */
        .timeline-item.running::before {{
            border-color: var(--status-running);
            box-shadow: 0 0 8px var(--status-running);
        }}
        .timeline-item.completed::before {{
            border-color: var(--status-completed);
        }}
        .timeline-item.failed::before {{
            border-color: var(--status-failed);
            box-shadow: 0 0 8px var(--status-failed);
        }}
        .timeline-item.skipped::before {{
            border-color: var(--status-skipped);
            opacity: 0.5;
        }}

        .item-content {{
            background: rgba(255, 255, 255, 0.02);
            border: 1px solid rgba(255, 255, 255, 0.03);
            border-radius: 12px;
            padding: 16px 20px;
            display: flex;
            flex-direction: column;
            gap: 8px;
        }}

        .item-header {{
            display: flex;
            justify-content: space-between;
            align-items: center;
        }}

        .node-name {{
            font-size: 16px;
            font-weight: 600;
            text-transform: capitalize;
        }}

        .status-pill {{
            font-size: 11px;
            font-weight: 700;
            text-transform: uppercase;
            padding: 3px 10px;
            border-radius: 6px;
            letter-spacing: 0.5px;
        }}

        .status-pill.running {{ background: rgba(99, 102, 241, 0.15); color: #a5b4fc; }}
        .status-pill.completed {{ background: rgba(16, 185, 129, 0.15); color: #6ee7b7; }}
        .status-pill.failed {{ background: rgba(239, 68, 68, 0.15); color: #fca5a5; }}
        .status-pill.skipped {{ background: rgba(107, 114, 128, 0.15); color: #d1d5db; opacity: 0.6; }}

        .time {{
            font-size: 12px;
            color: var(--text-secondary);
        }}

        .payload-msg {{
            font-size: 14px;
            color: #d1d5db;
        }}

        .findings-box {{
            background: rgba(0, 0, 0, 0.2);
            border-left: 3px solid #a78bfa;
            padding: 10px 14px;
            border-radius: 0 8px 8px 0;
            margin-top: 4px;
            font-size: 13px;
        }}

        .findings-box .source {{
            font-weight: 600;
            color: #c084fc;
            text-transform: uppercase;
            font-size: 10px;
            letter-spacing: 0.5px;
            margin-bottom: 2px;
        }}

        .findings-box .rc-label {{
            color: #f472b6;
            font-weight: 600;
        }}
    </style>
</head>
<body>
    <div class="container">
        <header>
            <div>
                <h1>Live Analysis Timeline</h1>
                <p style="color: var(--text-secondary); font-size: 14px; margin-top: 4px;">ID: <span id="analysis-id"></span></p>
            </div>
            <div id="connection-badge" class="ws-badge">
                <span class="dot"></span>
                <span id="connection-status">Connecting</span>
            </div>
        </header>
        
        <div id="timeline-list" class="timeline">
            <!-- Events populated dynamically -->
        </div>
    </div>

    <script>
        const analysisId = window.location.pathname.split('/').pop();
        document.getElementById('analysis-id').innerText = analysisId;
        
        const timelineList = document.getElementById('timeline-list');
        const badge = document.getElementById('connection-badge');
        const statusText = document.getElementById('connection-status');
        
        const ws = new WebSocket(
            `ws://${{location.host}}/ws/analysis/${{analysisId}}`
        );

        ws.onopen = () => {{
            badge.classList.add('connected');
            statusText.innerText = 'Connected';
            timelineList.innerHTML = '';
        }};

        ws.onclose = () => {{
            badge.classList.remove('connected');
            statusText.innerText = 'Disconnected';
        }};

        ws.onmessage = (event) => {{
            const data = JSON.parse(event.data);
            handleEvent(data);
        }};

        function handleEvent(ev) {{
            const node = ev.node || 'system';
            const status = ev.status || 'completed';
            const type = ev.event_type;
            const time = new Date(ev.timestamp).toLocaleTimeString();
            const payload = ev.payload || {{}};
            
            const existingId = `node-${{node}}-${{status}}-${{type.replace(/\./g, '-')}}`;
            if (document.getElementById(existingId)) return;

            const item = document.createElement('div');
            item.id = existingId;
            item.className = `timeline-item ${{status}}`;
            
            let payloadHtml = '';
            
            if (type === 'analysis.finding') {{
                if (payload.root_cause) {{
                    payloadHtml = `
                        <div class="findings-box" style="border-left-color: #f472b6;">
                            <div class="source" style="color: #f472b6;">ROOT CAUSE INFERRED</div>
                            <div><span class="rc-label">Cause:</span> ${{payload.root_cause}}</div>
                            <div style="margin-top: 2px;"><span class="rc-label">Confidence:</span> ${{(payload.confidence * 100).toFixed(0)}}%</div>
                        </div>
                    `;
                }} else {{
                    payloadHtml = `
                        <div class="findings-box">
                            <div class="source">Source: ${{payload.source}}</div>
                            <div>${{payload.message}}</div>
                        </div>
                    `;
                }}
            }} else {{
                let msg = payload.message || '';
                if (payload.error) {{
                    msg += `<div style="color: var(--status-failed); margin-top: 4px; font-family: monospace; font-size: 12px;">Error: ${{payload.error}}</div>`;
                }}
                if (payload.data && payload.data.confidence !== undefined) {{
                    msg += ` (Confidence: ${{(payload.data.confidence * 100).toFixed(0)}}%)`;
                }}
                payloadHtml = `<div class="payload-msg">${{msg}}</div>`;
            }}

            let nodeTitle = node.replace(/_/g, ' ');
            if (type === 'analysis.started') nodeTitle = 'Analysis Triggered';
            if (type === 'analysis.completed') nodeTitle = 'Analysis Finalized';
            if (type === 'analysis.failed') nodeTitle = 'Analysis Failed';

            item.innerHTML = `
                <div class="item-content">
                    <div class="item-header">
                        <span class="node-name">${{nodeTitle}}</span>
                        <span class="status-pill ${{status}}">${{status}}</span>
                    </div>
                    ${{payloadHtml}}
                    <div class="time">${{time}}</div>
                </div>
            `;
            
            timelineList.appendChild(item);
            item.scrollIntoView({{ behavior: 'smooth', block: 'nearest' }});
        }}
    </script>
</body>
</html>"""
    return HTMLResponse(content=html_content)

# ── Request / Response models ──────────────────────────────────────────

class AnalysisContext(BaseModel):
    recent_deployments: Optional[List[Dict[str, Any]]] = None
    ongoing_incidents: Optional[List[str]] = None

class AnalysisRequest(BaseModel):
    alert_id: str
    alert: Dict[str, Any]
    triggered_at: Optional[str] = None
    context: Optional[AnalysisContext] = None

class AnalysisResponse(BaseModel):
    analysis_id: str
    status: str
    message: str


# ── Endpoints ──────────────────────────────────────────────────────────

@app.get("/api/v1/health")
async def health():
    return {
        "status": "ok",
        "active_analyses": 0,
        "queue_depth": 0
    }


@app.post("/api/v1/analyses", response_model=AnalysisResponse, status_code=202)
async def start_analysis(req: AnalysisRequest):
    analysis_id = f"analysis-{uuid.uuid4()}"

    initial_state = {
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

    result = run_analysis(initial_state)

    return AnalysisResponse(
        analysis_id=analysis_id,
        status=result.get("status", "completed"),
        message="Analysis completed" if result.get("status") == "completed" else "Analysis queued"
    )