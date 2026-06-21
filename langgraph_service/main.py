from fastapi import FastAPI

from workflow.graph import run_analysis
from api.routes import interrupt

app = FastAPI(title="LangGraph SRE Copilot")

# Register human interrupt router
app.include_router(interrupt.router)


@app.get("/api/v1/health")
async def health():
    return {
        "status": "ok",
        "active_analyses": 0,
        "queue_depth": 0
    }


@app.post("/api/v1/analyses")
async def start_analysis():
    initial_state = {
        "analysis_id": "analysis_001",
        "alert": {
            "id": "alert_001",
            "name": "High Error Rate",
            "severity": "critical",
            "affected_services": ["payment-api"],
            "fired_at": "2026-06-20T12:00:00Z"
        },
        "incident_id": None,
        "findings": [],
        "current_agent": "supervisor",
        "status": "running",
        "report": None
    }

    result = run_analysis(initial_state)
    return result