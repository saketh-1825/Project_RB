import os
from typing import Any, Dict, List

from schemas.state import AnalysisState
from internal.client.go_backend import GoBackendClient
from internal.errors import GoBackendError
from agents.helpers import build_degraded_finding


def correlation_agent_node(state: AnalysisState) -> AnalysisState:
    """
    Correlates findings from log_query_agent and rag_agent with
    historical incidents to identify patterns and probable root cause.
    """
    # Pass-through if correlation findings already exist (resuming execution)
    findings = state.get("findings", [])
    if findings:
        correlation_findings = [f for f in findings if f.get("agent") == "correlation_agent"]
        if correlation_findings:
            state["current_agent"] = "report_agent"
            return state

    base_url = os.environ.get("GO_BACKEND_URL", "http://mock-go-backend:8080/api/v1")
    token = os.environ.get("SRE_INTERNAL_TOKEN", "mock-token")
    client = GoBackendClient(base_url=base_url, token=token)

    # 1. Gather existing findings from upstream agents
    log_findings = [f for f in findings if f.get("agent") == "log_query_agent"]
    rag_findings = [f for f in findings if f.get("agent") == "rag_agent"]

    # Extract affected services from state
    alert = state.get("alert") or {}
    affected_services = alert.get("affected_services", [])

    # Also extract from services topology if available
    topology = state.get("services_topology")
    degraded_services = []
    if topology and isinstance(topology, dict):
        for svc in topology.get("services", []):
            if isinstance(svc, dict) and svc.get("health") in ("degraded", "down"):
                degraded_services.append(svc.get("name", svc.get("service_id", "")))

    # 2. Look up past incidents for correlation
    similar_past_incidents = []
    try:
        incidents_resp = client._request("GET", "/api/v1/incidents")
        incidents_data = incidents_resp.json()
        past_incidents = incidents_data.get("incidents", [])

        # Find incidents involving the same affected services
        for past in past_incidents:
            if not isinstance(past, dict):
                continue
            past_services = set(past.get("affected_services", []))
            current_services = set(affected_services + degraded_services)
            overlap = past_services & current_services
            if overlap:
                similar_past_incidents.append({
                    "incident_id": past.get("incident_id"),
                    "title": past.get("title"),
                    "similarity_score": round(len(overlap) / max(len(current_services), 1), 2),
                    "resolution": past.get("root_cause_summary", "No resolution recorded")
                })
    except (GoBackendError, Exception):
        pass

    # 3. Build correlation analysis
    root_cause_description = _infer_root_cause(log_findings, rag_findings, degraded_services)
    confidence = _calculate_confidence(log_findings, rag_findings, similar_past_incidents)

    correlation = {
        "root_cause": {
            "description": root_cause_description,
            "affected_services": list(set(affected_services + degraded_services)),
            "confidence": confidence,
        },
        "similar_past_incidents": similar_past_incidents,
        "degraded_services": degraded_services,
    }
    state["correlation"] = correlation

    # 4. Build finding
    finding = {
        "agent": "correlation_agent",
        "type": "historical_correlation",
        "severity": "high" if confidence >= 0.7 else "medium",
        "title": "Root cause correlation analysis",
        "summary": root_cause_description,
        "confidence": confidence,
        "evidence": {
            "similar_past_incidents": [p.get("incident_id") for p in similar_past_incidents],
            "degraded_services": degraded_services,
            "log_finding_count": len(log_findings),
            "runbook_match_count": len(rag_findings),
        }
    }

    if "findings" not in state or not isinstance(state["findings"], list):
        state["findings"] = []
    state["findings"].append(finding)

    # Build event
    event = {
        "source": "correlation_agent",
        "event_type": "historical_correlation",
        "message": f"Correlation analysis completed — confidence {confidence}",
        "details": finding
    }
    if "incident_events" not in state or not isinstance(state["incident_events"], list):
        state["incident_events"] = []
    state["incident_events"].append(event)

    # Post finding to backend
    incident_id = state.get("incident_id")
    if incident_id:
        try:
            client.post_finding(incident_id, finding)
        except Exception:
            pass

    state["current_agent"] = "report_agent"
    return state


def _infer_root_cause(
    log_findings: List[Dict[str, Any]],
    rag_findings: List[Dict[str, Any]],
    degraded_services: List[str]
) -> str:
    """Build a root cause description from available evidence."""
    parts = []

    # Use log findings for error context
    for f in log_findings:
        if f.get("type") == "log_anomaly":
            parts.append(f"Error spike detected: {f.get('summary', 'unknown errors')}")

    # Use runbook matches for resolution context
    for f in rag_findings:
        if f.get("type") == "runbook":
            parts.append(f"Matched runbook: {f.get('title', 'unknown runbook')}")

    # Use degraded services
    if degraded_services:
        parts.append(f"Degraded services: {', '.join(degraded_services)}")

    if not parts:
        return "Insufficient evidence to determine root cause"

    return "; ".join(parts)


def _calculate_confidence(
    log_findings: List[Dict[str, Any]],
    rag_findings: List[Dict[str, Any]],
    similar_incidents: List[Dict[str, Any]]
) -> float:
    """Calculate confidence score based on available evidence."""
    score = 0.3  # Base confidence

    # Boost for log anomaly evidence
    if any(f.get("type") == "log_anomaly" for f in log_findings):
        score += 0.25

    # Boost for runbook match
    if any(f.get("type") == "runbook" for f in rag_findings):
        score += 0.2

    # Boost for similar past incidents
    if similar_incidents:
        score += min(0.25, 0.1 * len(similar_incidents))

    return round(min(score, 1.0), 2)
