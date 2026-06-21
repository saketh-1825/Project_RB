from fastapi import APIRouter, status
from fastapi.responses import JSONResponse
from pydantic import BaseModel
from typing import Dict, Any

from internal.redis_client import get_analysis_state
from workflow.graph import resume_analysis

router = APIRouter()

class InterruptRequest(BaseModel):
    interrupt_type: str
    payload: Dict[str, Any]
    provided_by: str

@router.post("/api/v1/analyses/{analysis_id}/interrupt")
async def interrupt_analysis(analysis_id: str, req: InterruptRequest):
    # 1. Retrieve the analysis state from Redis
    state = get_analysis_state(analysis_id)
    
    # 2. Validate that the analysis exists
    if not state:
        return JSONResponse(
            status_code=status.HTTP_409_CONFLICT,
            content={
                "error": {
                    "code": "ANALYSIS_NOT_AWAITING_HUMAN",
                    "message": "Analysis does not exist."
                }
            }
        )
        
    # 3. Validate that status is "awaiting_human"
    if state.get("status") != "awaiting_human":
        return JSONResponse(
            status_code=status.HTTP_409_CONFLICT,
            content={
                "error": {
                    "code": "ANALYSIS_NOT_AWAITING_HUMAN",
                    "message": "Analysis is not awaiting human decision."
                }
            }
        )

    # 4. Inject human context from payload and set last_interrupted_at timestamp
    from datetime import datetime, timezone
    message = req.payload.get("message") or ""
    state["human_context"] = message
    state["awaiting_human"] = False
    state["status"] = "running"
    state["last_interrupted_at"] = datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")

    # 5. Resume execution using graph helper
    resumed_state = resume_analysis(state)

    # 6. Return the final result
    return resumed_state
