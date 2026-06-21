import os
from datetime import datetime, timezone
from typing import Any, Dict, List

from schemas.state import AnalysisState
from internal.client.go_backend import GoBackendClient
from internal.errors import GoBackendError
from agents.helpers import build_degraded_finding


def report_agent_node(state: AnalysisState) -> AnalysisState:
    """
    Synthesizes all findings from the analysis into a structured
    IncidentReport and submits it to the Go backend.
    """
    base_url = os.environ.get("GO_BACKEND_URL", "http://mock-go-backend:8080/api/v1")
    token = os.environ.get("SRE_INTERNAL_TOKEN", "mock-token")
    client = GoBackendClient(base_url=base_url, token=token)

    findings = state.get("findings", [])
    incident_events = state.get("incident_events", [])
    incident_id = state.get("incident_id")
    alert = state.get("alert") or {}
    correlation = state.get("correlation") or {}

    # 1. Build timeline from incident events
    timeline = []
    for event in incident_events:
        if not isinstance(event, dict):
            continue
        timeline.append({
            "timestamp": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
            "event": event.get("message", "Unknown event"),
            "source": event.get("source", "unknown")
        })

    # 2. Build root cause section from correlation data
    root_cause_data = correlation.get("root_cause", {})
    root_cause = {
        "description": root_cause_data.get("description", "Root cause undetermined"),
        "affected_services": root_cause_data.get("affected_services", alert.get("affected_services", [])),
        "confidence": root_cause_data.get("confidence", 0.0),
        "supporting_findings": [
            {
                "agent": f.get("agent"),
                "type": f.get("type"),
                "title": f.get("title"),
                "summary": f.get("summary"),
            }
            for f in findings if not f.get("degraded")
        ]
    }

    # 3. Build suggested fixes from runbook findings
    suggested_fixes = []
    runbook_findings = [f for f in findings if f.get("type") == "runbook"]
    for i, rb in enumerate(runbook_findings):
        suggested_fixes.append({
            "priority": i + 1,
            "action": f"Follow runbook: {rb.get('title', 'Unknown')}",
            "rationale": rb.get("summary", "See matched runbook for details"),
            "runbook_reference": rb.get("runbook_id"),
            "risk_level": "low"
        })

    # Default fix if none from runbooks
    if not suggested_fixes:
        suggested_fixes.append({
            "priority": 1,
            "action": "Investigate manually — no runbook matched",
            "rationale": "Automated analysis did not find a matching runbook",
            "runbook_reference": None,
            "risk_level": "medium"
        })

    # 4. Build similar past incidents
    similar_past = correlation.get("similar_past_incidents", [])

    # 5. Collect runbooks consulted
    runbooks_consulted = []
    for f in runbook_findings:
        runbooks_consulted.append({
            "runbook_id": f.get("runbook_id"),
            "title": f.get("title"),
            "similarity_score": f.get("similarity_score"),
        })

    # 6. Assemble the full IncidentReport
    report = {
        "incident_id": incident_id or "unknown",
        "alert_id": alert.get("alert_id") or alert.get("id") or state.get("alert_id", "unknown"),
        "generated_at": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
        "title": state.get("incident_title", "Incident Report"),
        "executive_summary": _build_executive_summary(state, findings, correlation),
        "root_cause": root_cause,
        "timeline": timeline,
        "suggested_fixes": suggested_fixes,
        "similar_past_incidents": similar_past,
        "runbooks_consulted": runbooks_consulted,
        "model_metadata": {
            "total_tokens_used": 0,
            "agents_invoked": list({f.get("agent") for f in findings if f.get("agent")}),
            "analysis_duration_ms": 0
        }
    }

    state["report"] = report

    # 7. Build finding for the report agent
    finding = {
        "agent": "report_agent",
        "type": "incident_report",
        "severity": "info",
        "title": "Incident report generated",
        "summary": report["executive_summary"],
        "confidence": root_cause.get("confidence", 0.0),
    }

    if "findings" not in state or not isinstance(state["findings"], list):
        state["findings"] = []
    state["findings"].append(finding)

    event = {
        "source": "report_agent",
        "event_type": "report_generated",
        "message": "Final incident report generated",
        "details": {"report_title": report["title"]}
    }
    if "incident_events" not in state or not isinstance(state["incident_events"], list):
        state["incident_events"] = []
    state["incident_events"].append(event)

    # 8. Submit report to Go backend
    if incident_id:
        try:
            client.submit_report(incident_id, report)
        except GoBackendError:
            pass

    state["current_agent"] = "report_agent"
    state["status"] = "completed"
    state["awaiting_human"] = False

    return state


def _build_executive_summary(
    state: AnalysisState,
    findings: List[Dict[str, Any]],
    correlation: Dict[str, Any]
) -> str:
    """Build a concise executive summary from the analysis results."""
    title = state.get("incident_title", "Unknown Incident")
    summary = state.get("incident_summary", "")

    root_cause_desc = correlation.get("root_cause", {}).get("description", "undetermined")
    confidence = correlation.get("root_cause", {}).get("confidence", 0.0)

    total_findings = len([f for f in findings if not f.get("degraded")])
    degraded_count = len([f for f in findings if f.get("degraded")])

    parts = [
        f"Incident: {title}.",
        f"Summary: {summary}." if summary else "",
        f"Root cause analysis (confidence {confidence}): {root_cause_desc}.",
        f"Total findings: {total_findings}.",
    ]

    if degraded_count:
        parts.append(f"Note: {degraded_count} agent(s) ran in degraded mode due to backend unavailability.")

    similar_count = len(correlation.get("similar_past_incidents", []))
    if similar_count:
        parts.append(f"{similar_count} similar past incident(s) found.")

    return " ".join(p for p in parts if p)
