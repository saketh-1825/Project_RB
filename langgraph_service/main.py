import uuid
from typing import Any, Dict, List, Optional

from fastapi import FastAPI
from pydantic import BaseModel

from workflow.graph import run_analysis
from api.routes import interrupt

app = FastAPI(title="LangGraph SRE Copilot")

# Register human interrupt router
app.include_router(interrupt.router)


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