import os
from datetime import datetime, timedelta
from typing import Any, Dict, List

from schemas.state import AnalysisState
from internal.client.go_backend import GoBackendClient

def extract_log_ids(logs: List[Dict[str, Any]]) -> List[str]:
    if not isinstance(logs, list):
        return []
    return [str(log["id"]) for log in logs if isinstance(log, dict) and "id" in log and log["id"] is not None]

def extract_trace_ids(logs: List[Dict[str, Any]]) -> List[str]:
    if not isinstance(logs, list):
        return []
    trace_ids = set()
    for log in logs:
        if isinstance(log, dict):
            t_id = log.get("trace_id")
            if t_id is not None:
                trace_ids.add(str(t_id))
    return list(trace_ids)

def build_finding(log_ids: List[str], trace_ids: List[str]) -> Dict[str, Any]:
    return {
        "agent": "log_query_agent",
        "type": "log_anomaly",
        "severity": "high",
        "title": "Error spike detected",
        "summary": "Large number of ERROR logs found",
        "evidence": {
            "log_ids": log_ids if isinstance(log_ids, list) else [],
            "trace_ids": trace_ids if isinstance(trace_ids, list) else []
        },
        "confidence": 0.9
    }

def log_query_agent_node(state: AnalysisState) -> AnalysisState:
    base_url = os.environ.get("GO_BACKEND_URL", "http://mock-go-backend:8080/api/v1")
    token = os.environ.get("SRE_INTERNAL_TOKEN", "mock-token")
    client = GoBackendClient(base_url=base_url, token=token)

    alert = state.get("alert", {})
    affected_services = alert.get("affected_services", [])
    fired_at_str = alert.get("fired_at")
    
    if fired_at_str:
        clean_time_str = fired_at_str.replace("Z", "+00:00")
        try:
            fired_at = datetime.fromisoformat(clean_time_str)
        except ValueError:
            fired_at = datetime.utcnow()
    else:
        fired_at = datetime.utcnow()

    from_time_str = (fired_at - timedelta(minutes=10)).isoformat().replace("+00:00", "Z")
    to_time_str = fired_at.isoformat().replace("+00:00", "Z")

    try:
        logs_resp = client.get_logs(
            from_time=from_time_str,
            to_time=to_time_str,
            services=affected_services,
            levels=["ERROR", "FATAL"]
        )
        logs = logs_resp.get("logs") if logs_resp else []
        if not isinstance(logs, list):
            logs = []
    except Exception:
        logs = []

    try:
        client.get_log_anomalies(
            from_time=from_time_str,
            to_time=to_time_str,
            services=affected_services
        )
    except Exception:
        pass

    trace_ids = extract_trace_ids(logs)
    for trace_id in trace_ids:
        try:
            client.get_trace(trace_id)
        except Exception:
            pass

    log_ids = extract_log_ids(logs)
    finding = build_finding(log_ids, trace_ids)

    if "findings" not in state or not isinstance(state["findings"], list):
        state["findings"] = []
    
    state["findings"].append(finding)

    incident_id = state.get("incident_id")
    if incident_id:
        try:
            client.post_finding(incident_id, finding)
        except Exception:
            pass

    state["current_agent"] = "rag_agent"

    return state
