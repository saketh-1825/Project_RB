import os
from datetime import datetime, timedelta, timezone
from typing import Any

import logging
from internal.llm_client import call_llm
from internal.client.go_backend import GoBackendClient
from schemas.state import AnalysisState

logger = logging.getLogger(__name__)


def build_timeline(state: AnalysisState) -> list[dict[str, Any]]:
    """
    Constructs a chronological timeline from alert fired_at, metric spikes, log/trace/runbook/correlation findings, and report generation time.
    """
    from internal.correlation.engine import find_spike_time

    alert = state.get("alert") or {}
    fired_at_str = alert.get("fired_at")

    alert_time = None
    if fired_at_str:
        clean_time_str = fired_at_str.replace("Z", "+00:00")
        try:
            alert_time = datetime.fromisoformat(clean_time_str)
            if alert_time.tzinfo is None:
                alert_time = alert_time.replace(tzinfo=timezone.utc)
        except ValueError:
            pass

    if not alert_time:
        alert_time = datetime.now(timezone.utc)

    raw_events = []

    def parse_datetime(t_str):
        if not t_str:
            return None
        if isinstance(t_str, datetime):
            return t_str if t_str.tzinfo else t_str.replace(tzinfo=timezone.utc)
        try:
            clean = str(t_str).replace("Z", "+00:00")
            parsed = datetime.fromisoformat(clean)
            return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)
        except Exception:  # noqa: BLE001
            return None

    def format_z(dt):
        return dt.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")

    # 1. Metric Spikes
    metrics_data = state.get("metrics_data") or {}
    for metric_name, series in metrics_data.items():
        spike_dt = find_spike_time(series)
        if spike_dt:
            parsed_spike = parse_datetime(spike_dt)
            if parsed_spike:
                raw_events.append(
                    {"dt": parsed_spike, "event": f"{metric_name} started increasing"}
                )

    # 2. Alert fired_at
    if fired_at_str:
        raw_events.append(
            {"dt": alert_time, "event": f"{alert.get('name', 'Alert')} alert fired"}
        )

    # Findings (Log anomalies, trace anomalies, runbook findings, correlation findings)
    findings = state.get("findings", [])
    for idx, finding in enumerate(findings):
        # Skip report agent findings to avoid self-reference
        if finding.get("agent") == "report_agent":
            continue

        t_val = finding.get("created_at") or finding.get("timestamp")
        dt_val = parse_datetime(t_val)
        if not dt_val:
            dt_val = alert_time + timedelta(seconds=idx + 1)

        f_type = finding.get("type", "")
        f_agent = finding.get("agent", "")

        if f_type == "log_anomaly" or f_agent == "log_query_agent":
            event_text = (
                finding.get("summary") or finding.get("title") or "Log anomaly detected"
            )
        elif f_type == "trace_anomaly" or "trace" in f_type:
            event_text = (
                finding.get("summary")
                or finding.get("title")
                or "Trace anomaly detected"
            )
        elif f_type == "runbook" or f_agent == "rag_agent":
            event_text = f"Runbook matched: {finding.get('title', 'Unknown')}"
        elif f_type == "historical_correlation" or f_agent == "correlation_agent":
            event_text = (
                finding.get("summary")
                or finding.get("title")
                or "Correlation analysis completed"
            )
        else:
            event_text = (
                finding.get("summary")
                or finding.get("title")
                or f"Finding from {f_agent}"
            )

        raw_events.append({"dt": dt_val, "event": event_text})

    # Report Generation (current time)
    report_gen_time = datetime.now(timezone.utc)
    raw_events.append({"dt": report_gen_time, "event": "Incident report generated"})

    # Sort ascending
    raw_events.sort(key=lambda x: x["dt"])

    # Build timeline list
    timeline = []
    for e in raw_events:
        timeline.append({"time": format_z(e["dt"]), "event": e["event"]})
    return timeline


def build_root_cause(state: AnalysisState) -> dict[str, Any]:
    """
    Directly consumes state["root_cause"] and aggregates all non-degraded findings.
    """
    rc_data = state.get("root_cause") or {}
    findings = state.get("findings", [])

    supporting_findings = []
    for f in findings:
        if f.get("degraded") or f.get("agent") == "report_agent":
            continue
        supporting_findings.append(
            {
                "agent": f.get("agent"),
                "type": f.get("type"),
                "title": f.get("title"),
                "summary": f.get("summary") or f.get("description") or "",
            }
        )

    # Extract metrics summary and logs for better description
    rc_desc = rc_data.get("description", "Root cause undetermined")

    # Try to build a more meaningful description if we have correlation data
    correlation = state.get("correlation") or {}
    confidence_data = correlation.get("confidence") or {}
    positive_factors = confidence_data.get("positive_factors", [])

    enhanced_desc = rc_desc
    if positive_factors and rc_data.get("type") != "UNKNOWN":
        factors_str = " and ".join(
            [f.split(" (+")[0].lower() for f in positive_factors[:2]]
        )
        if factors_str:
            enhanced_desc = (
                f"{rc_desc.rstrip('.')}. This is likely because {factors_str}."
            )

    return {
        "description": enhanced_desc,
        "affected_services": rc_data.get("affected_services", []),
        "confidence": rc_data.get("confidence", 0.0),
        "supporting_findings": supporting_findings,
    }


def extract_runbook_fixes(state: AnalysisState) -> list[dict[str, Any]]:
    """
    Filters findings for runbooks, sorts them by similarity_score descending,
    and returns them with sequential priority.
    """
    findings = state.get("findings", [])
    runbook_findings = [f for f in findings if f.get("type") == "runbook"]

    # Sort descending by similarity_score
    runbook_findings.sort(
        key=lambda x: float(x.get("similarity_score", 0.0)), reverse=True
    )

    fixes = []
    for idx, rb in enumerate(runbook_findings):
        # We map to the new structured format while preserving old fields for any downstream legacy code
        priority_level = "HIGH" if idx == 0 else ("MEDIUM" if idx == 1 else "LOW")
        fixes.append(
            {
                "action": rb.get("title", "Execute runbook steps"),
                "reason": rb.get("summary")
                or rb.get("content")
                or "Matched historical mitigation",
                "priority": priority_level,
                # Legacy fields for backward compatibility
                "priority_rank": idx + 1,
                "title": rb.get("title", "Runbook Fix"),
                "description": rb.get("summary")
                or rb.get("content")
                or "Execute runbook steps",
                "runbook_id": rb.get("runbook_id"),
                "similarity_score": float(rb.get("similarity_score", 0.0)),
            }
        )
    return fixes


def build_suggested_fixes(state: AnalysisState) -> list[dict[str, Any]]:
    """
    Builds suggested fixes. Falls back if no runbooks are matched.
    """
    fixes = extract_runbook_fixes(state)
    if not fixes:
        return [
            {
                "action": "Investigate root cause manually",
                "reason": "No matching runbooks were found",
                "priority": "HIGH",
                "priority_rank": 1,
                "title": "Investigate root cause",
                "description": "No runbook found.",
            }
        ]
    fixes.sort(key=lambda x: x.get("priority_rank", 1))
    return fixes


def build_executive_summary(
    state: AnalysisState, suggested_fixes: list[dict[str, Any]]
) -> str:
    """
    Generates an executive summary using an LLM to synthesize all evidence
    into a human-readable narrative. Falls back to a deterministic template
    if the LLM is unavailable or returns an empty response.
    """
    rc_data = state.get("root_cause") or {}
    alert = state.get("alert") or {}
    correlation = state.get("correlation") or {}
    confidence_data = correlation.get("confidence") or {}
    evidence_quality = correlation.get("evidence_quality") or {}

    services = rc_data.get("affected_services") or alert.get("affected_services", [])
    services_str = ", ".join(services) if services else "unknown services"
    rc_type = rc_data.get("type", "UNKNOWN")
    rc_desc = rc_data.get("description", "an undetermined issue")
    confidence_score = confidence_data.get("score", 0.0)
    confidence_level = confidence_data.get("level", "LOW")
    available_sources = evidence_quality.get("available_sources", [])
    top_fix = suggested_fixes[0].get("action", "manual investigation") if suggested_fixes else "manual investigation"
    human_context = state.get("human_context", "")

    # Build LLM prompt
    prompt = f"""You are an expert SRE writing a concise executive summary for an incident report.
Write exactly 3 sentences. Be specific, technical, and actionable.

INCIDENT DATA:
- Alert: {alert.get("name", "Unknown alert")}
- Affected services: {services_str}
- Root cause type: {rc_type}
- Root cause description: {rc_desc}
- Confidence: {confidence_level} ({confidence_score})
- Evidence sources available: {", ".join(available_sources) if available_sources else "none"}
- Recommended action: {top_fix}
{f"- Operator context: {human_context}" if human_context else ""}

Write only the 3-sentence summary. No headers, no bullet points, no preamble."""

    llm_summary = call_llm(prompt, max_tokens=500)

    if llm_summary:
        logger.info("Executive summary generated by LLM")
        return llm_summary

    # Deterministic fallback if LLM is unavailable
    logger.info("LLM unavailable — using deterministic executive summary")
    level = confidence_data.get("level", "UNKNOWN")
    score = confidence_data.get("score", 0.0)
    evidence_parts = []
    if "Strong log evidence found" in confidence_data.get("reason", ""):
        evidence_parts.append("ERROR log spikes")
    if "Metric anomaly detected" in confidence_data.get("reason", ""):
        evidence_parts.append("elevated metrics")
    if "runbook" in confidence_data.get("reason", "").lower():
        evidence_parts.append("matching historical runbook")
    evidence_str = ""
    if evidence_parts:
        joined = ", ".join(evidence_parts[:-1])
        last = evidence_parts[-1]
        evidence_str = f" Evidence includes {f'{joined} and {last}' if joined else last}."
    s1 = f"{services_str.capitalize()} experienced issues likely caused by {rc_desc.lower().rstrip('.')}."
    s2 = f" Confidence: {level} ({score}).{evidence_str}"
    s3 = f" Recommended action: {top_fix}." if top_fix != "manual investigation" else " No matching runbooks found — manual investigation required."
    return (s1 + s2 + s3).strip()


def build_incident_report(
    state: AnalysisState,
    timeline: list[dict[str, Any]],
    root_cause: dict[str, Any],
    suggested_fixes: list[dict[str, Any]],
    executive_summary: str,
) -> dict[str, Any]:
    """
    Builds the final IncidentReport dictionary.
    """
    created_at = datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")
    alert = state.get("alert") or {}

    correlation = state.get("correlation") or {}
    confidence_data = correlation.get("confidence") or {}
    evidence_quality = correlation.get("evidence_quality") or {}
    risk = correlation.get("risk") or {}

    return {
        "incident_id": state.get("incident_id") or "unknown",
        "title": state.get("incident_title") or alert.get("name") or "Incident Report",
        "executive_summary": executive_summary,
        "timeline": timeline,
        "root_cause": root_cause,
        "suggested_fixes": suggested_fixes,
        "confidence_explanation": confidence_data.get("explanation", ""),
        "evidence_summary": evidence_quality.get("available_sources", []),
        "missing_evidence": evidence_quality.get("missing_sources", []),
        "risk_assessment": risk,
        "created_at": created_at,
    }


def report_agent_node(state: AnalysisState) -> AnalysisState:
    try:
        return _report_agent_node_impl(state)
    except Exception:
        import traceback

        traceback.print_exc()
        raise


def _report_agent_node_impl(state: AnalysisState) -> AnalysisState:
    """
    Aggregates findings into an IncidentReport and submits it to the backend.
    """

    # 2. Build report sections
    timeline = build_timeline(state)
    root_cause = build_root_cause(state)
    suggested_fixes = build_suggested_fixes(state)
    executive_summary = build_executive_summary(state, suggested_fixes)
    incident_report = build_incident_report(
        state, timeline, root_cause, suggested_fixes, executive_summary
    )

    # Store report in state
    state["report"] = incident_report

    # 3. Submit report to Go backend if incident_id exists
    incident_id = state.get("incident_id")
    base_url = os.environ.get("GO_BACKEND_URL", "http://mock-go-backend:8080/api/v1")
    token = os.environ.get("SRE_INTERNAL_TOKEN", "mock-token")
    client = GoBackendClient(base_url=base_url, token=token)

    # Default status
    state["report_status"] = "FAILED_TO_SUBMIT"

    if incident_id:
        try:
            client.submit_report(incident_id, incident_report)
            state["report_status"] = "SUBMITTED"
        except Exception:  # noqa: BLE001
            state["report_status"] = "FAILED_TO_SUBMIT"

    # Add agent finding
    finding = {
        "agent": "report_agent",
        "type": "incident_report",
        "severity": "info",
        "title": "Incident report generated",
        "summary": incident_report["executive_summary"],
        "confidence": root_cause.get("confidence", 0.0),
    }

    if "findings" not in state or not isinstance(state["findings"], list):
        state["findings"] = []
    state["findings"].append(finding)

    event = {
        "source": "report_agent",
        "event_type": "report_generated",
        "message": "Final incident report generated",
        "details": {"report_title": incident_report["title"]},
    }
    if "incident_events" not in state or not isinstance(state["incident_events"], list):
        state["incident_events"] = []
    state["incident_events"].append(event)

    state["status"] = "completed"
    state["current_agent"] = "report_agent"
    state["awaiting_human"] = False

    return state
