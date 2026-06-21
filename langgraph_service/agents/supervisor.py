from schemas.state import AnalysisState

def supervisor_node(state: AnalysisState) -> AnalysisState:
    # Initialize collections and state fields
    if "findings" not in state or state["findings"] is None:
        state["findings"] = []
    if "incident_events" not in state or state["incident_events"] is None:
        state["incident_events"] = []
    if "resume_count" not in state or state["resume_count"] is None:
        state["resume_count"] = 0
    if "last_interrupted_at" not in state:
        state["last_interrupted_at"] = None

    # Extract or infer incident fields
    alert = state.get("alert") or {}
    incident_title = state.get("incident_title") or alert.get("name") or "Generated Incident"
    incident_summary = state.get("incident_summary") or alert.get("annotations", {}).get("description") or alert.get("summary") or "P95 latency exceeded threshold"
    
    # Store back to state
    state["incident_title"] = incident_title
    state["incident_summary"] = incident_summary
    
    # Store initial rag_query in state
    state["rag_query"] = f"{incident_title} {incident_summary}".strip()

    state["current_agent"] = "log_query_agent"
    state["status"] = "running"

    return state
