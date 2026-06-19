import os
from schemas.state import AnalysisState
from internal.client.go_backend import GoBackendClient

def supervisor_node(state: AnalysisState) -> AnalysisState:
    base_url = os.environ.get("GO_BACKEND_URL", "http://mock-go-backend:8080/api/v1")
    token = os.environ.get("SRE_INTERNAL_TOKEN", "mock-token")
    client = GoBackendClient(base_url=base_url, token=token)

    health_response = client.get_health()
    if health_response.get("status") != "ok" and health_response.get("health") != "ok":
        state["status"] = "failed"
        return state

    alert = state.get("alert", {})
    incident_data = {
        "title": alert.get("name", "Generated Incident"),
        "severity": alert.get("severity", "critical"),
        "status": "open",
        "alert_id": alert.get("alert_id")
    }
    
    incident_response = client.create_incident(incident_data)
    state["incident_id"] = incident_response.get("incident_id")

    services_response = client.get_services()
    state["services"] = services_response

    state["current_agent"] = "log_query_agent"
    state["status"] = "running"

    return state
