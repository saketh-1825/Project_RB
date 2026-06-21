import os
from schemas.state import AnalysisState
from internal.client.go_backend import GoBackendClient
from internal.errors import GoBackendError


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

    # ── Backend integration ────────────────────────────────────────────
    base_url = os.environ.get("GO_BACKEND_URL", "http://mock-go-backend:8080/api/v1")
    token = os.environ.get("SRE_INTERNAL_TOKEN", "mock-token")
    client = GoBackendClient(base_url=base_url, token=token)

    # 1. Health check — verify Go backend is alive
    try:
        health = client.get_health()
        state["backend_health"] = health.get("status", "unknown")
    except GoBackendError:
        state["backend_health"] = "unavailable"

    # 2. Load service topology
    try:
        services_resp = client.get_services()
        state["services_topology"] = services_resp
    except GoBackendError:
        state["services_topology"] = None

    # 3. Create incident — open a ticket before delegating to agents
    try:
        affected_services = alert.get("affected_services", [])
        incident_data = {
            "alert_id": alert.get("alert_id") or alert.get("id") or state.get("alert_id", "unknown"),
            "title": incident_title,
            "severity": alert.get("severity", "high"),
            "affected_services": affected_services,
            "opened_by": "supervisor_agent"
        }
        incident_resp = client.create_incident(incident_data)
        state["incident_id"] = incident_resp.get("incident_id")
    except GoBackendError:
        # If incident creation fails, downstream agents won't be able
        # to post findings to the backend, but the graph can still run
        state["incident_id"] = None

    state["current_agent"] = "log_query_agent"
    state["status"] = "running"

    return state
