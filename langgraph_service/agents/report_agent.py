import os
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List

from schemas.state import AnalysisState
from internal.client.go_backend import GoBackendClient
from internal.errors import GoBackendError


def build_timeline(state: AnalysisState) -> List[Dict[str, Any]]:
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
        except Exception:
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
                raw_events.append({
                    "dt": parsed_spike,
                    "event": f"{metric_name} started increasing"
                })

    # 2. Alert fired_at
    if fired_at_str:
        raw_events.append({
            "dt": alert_time,
            "event": f"{alert.get('name', 'Alert')} alert fired"
        })

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
            event_text = finding.get("summary") or finding.get("title") or "Log anomaly detected"
        elif f_type == "trace_anomaly" or "trace" in f_type:
            event_text = finding.get("summary") or finding.get("title") or "Trace anomaly detected"
        elif f_type == "runbook" or f_agent == "rag_agent":
            event_text = f"Runbook matched: {finding.get('title', 'Unknown')}"
        elif f_type == "historical_correlation" or f_agent == "correlation_agent":
            event_text = finding.get("summary") or finding.get("title") or "Correlation analysis completed"
        else:
            event_text = finding.get("summary") or finding.get("title") or f"Finding from {f_agent}"

        raw_events.append({
            "dt": dt_val,
            "event": event_text
        })

    # Report Generation (current time)
    report_gen_time = datetime.now(timezone.utc)
    raw_events.append({
        "dt": report_gen_time,
        "event": "Incident report generated"
    })

    # Sort ascending
    raw_events.sort(key=lambda x: x["dt"])

    # Build timeline list
    timeline = []
    for e in raw_events:
        timeline.append({
            "time": format_z(e["dt"]),
            "event": e["event"]
        })
    return timeline


def build_root_cause(state: AnalysisState) -> Dict[str, Any]:
    """
    Directly consumes state["root_cause"] and aggregates all non-degraded findings.
    """
    rc_data = state.get("root_cause") or {}
    findings = state.get("findings", [])

    supporting_findings = []
    for f in findings:
        if f.get("degraded") or f.get("agent") == "report_agent":
            continue
        supporting_findings.append({
            "agent": f.get("agent"),
            "type": f.get("type"),
            "title": f.get("title"),
            "summary": f.get("summary") or f.get("description") or ""
        })

    return {
        "description": rc_data.get("description", "Root cause undetermined"),
        "affected_services": rc_data.get("affected_services", []),
        "confidence": rc_data.get("confidence", 0.0),
        "supporting_findings": supporting_findings
    }


def extract_runbook_fixes(state: AnalysisState) -> List[Dict[str, Any]]:
    """
    Filters findings for runbooks, sorts them by similarity_score descending,
    and returns them with sequential priority.
    """
    findings = state.get("findings", [])
    runbook_findings = [f for f in findings if f.get("type") == "runbook"]

    # Sort descending by similarity_score
    runbook_findings.sort(key=lambda x: float(x.get("similarity_score", 0.0)), reverse=True)

    fixes = []
    for idx, rb in enumerate(runbook_findings):
        fixes.append({
            "priority": idx + 1,
            "title": rb.get("title", "Runbook Fix"),
            "description": rb.get("summary") or rb.get("content") or "Execute runbook steps",
            "runbook_id": rb.get("runbook_id"),
            "similarity_score": float(rb.get("similarity_score", 0.0))
        })
    return fixes


def build_suggested_fixes(state: AnalysisState) -> List[Dict[str, Any]]:
    """
    Builds suggested fixes. Falls back if no runbooks are matched.
    """
    fixes = extract_runbook_fixes(state)
    if not fixes:
        return [
            {
                "priority": 1,
                "title": "Investigate root cause",
                "description": "No runbook found."
            }
        ]
    fixes.sort(key=lambda x: x["priority"])
    return fixes


def build_executive_summary(state: AnalysisState, suggested_fixes: List[Dict[str, Any]]) -> str:
    """
    Builds a concise, dynamic executive summary from metrics_summary, root_cause, suggested_fixes, and affected_services.
    """
    from internal.correlation.engine import find_spike_time

    rc_data = state.get("root_cause") or {}
    alert = state.get("alert") or {}

    # 1. Affected services & Root cause
    services = rc_data.get("affected_services") or alert.get("affected_services", [])
    services_str = ", ".join(services) if services else "unknown services"
    rc_desc = rc_data.get("description", "an undetermined issue").lower().rstrip(".")
    s1 = f"High error rates affected {services_str}. The root cause was {rc_desc}."

    # 2. Metric spike lag
    s2 = ""
    metrics_data = state.get("metrics_data") or {}

    err_spike = find_spike_time(metrics_data.get("error_rate") or metrics_data.get("http_error_rate"))
    db_spike = find_spike_time(metrics_data.get("db_pool_waiting") or metrics_data.get("db_pool_waiting_connections"))

    def parse_datetime(t_str):
        if not t_str:
            return None
        if isinstance(t_str, datetime):
            return t_str if t_str.tzinfo else t_str.replace(tzinfo=timezone.utc)
        try:
            clean = str(t_str).replace("Z", "+00:00")
            parsed = datetime.fromisoformat(clean)
            return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)
        except Exception:
            return None

    dt_err_spike = parse_datetime(err_spike)
    dt_db_spike = parse_datetime(db_spike)

    if dt_db_spike and dt_err_spike and dt_err_spike > dt_db_spike:
        lag = int((dt_err_spike - dt_db_spike).total_seconds())
        s2 = f"db_pool_waiting increased {lag} seconds before error_rate spike."

    # 3. Peak metrics
    metrics_summary = state.get("metrics_summary") or {}
    err_max = metrics_summary.get("error_rate", {}).get("max")
    cpu_max = metrics_summary.get("cpu", {}).get("max")
    mem_max = metrics_summary.get("memory", {}).get("max")

    peak_parts = []
    if err_max is not None:
        peak_parts.append(f"Error rate peaked at {err_max}%.")
    if cpu_max is not None:
        peak_parts.append(f"CPU peaked at {cpu_max}%.")
    if mem_max is not None:
        peak_parts.append(f"Memory peaked at {mem_max}%.")
    s3 = " ".join(peak_parts)

    # 4. Top remediation
    s4 = ""
    runbook_fixes = [f for f in suggested_fixes if f.get("runbook_id")]
    if runbook_fixes:
        top_fix_title = runbook_fixes[0].get("title", "")
        top_fix_title = top_fix_title.rstrip(".")
        s4 = f"Top remediation: {top_fix_title}."
    else:
        s4 = "No runbooks were found, requiring manual investigation."

    parts = [s1, s2, s3, s4]
    return " ".join([p for p in parts if p])


def build_incident_report(
    state: AnalysisState,
    timeline: List[Dict[str, Any]],
    root_cause: Dict[str, Any],
    suggested_fixes: List[Dict[str, Any]],
    executive_summary: str
) -> Dict[str, Any]:
    """
    Builds the final IncidentReport dictionary.
    """
    created_at = datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")
    alert = state.get("alert") or {}

    return {
        "incident_id": state.get("incident_id") or "unknown",
        "title": state.get("incident_title") or alert.get("name") or "Incident Report",
        "executive_summary": executive_summary,
        "timeline": timeline,
        "root_cause": root_cause,
        "suggested_fixes": suggested_fixes,
        "created_at": created_at
    }


def report_agent_node(state: AnalysisState) -> AnalysisState:
    """
    Aggregates findings into an IncidentReport and submits it to the backend.
    """
    from prompts import load_prompt

    # 1. Load prompt template
    try:
        load_prompt("report_agent.txt")
    except Exception:
        pass

    # 2. Build report sections
    timeline = build_timeline(state)
    root_cause = build_root_cause(state)
    suggested_fixes = build_suggested_fixes(state)
    executive_summary = build_executive_summary(state, suggested_fixes)
    incident_report = build_incident_report(state, timeline, root_cause, suggested_fixes, executive_summary)

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
        except Exception:
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
        "details": {"report_title": incident_report["title"]}
    }
    if "incident_events" not in state or not isinstance(state["incident_events"], list):
        state["incident_events"] = []
    state["incident_events"].append(event)

    state["status"] = "completed"
    state["current_agent"] = "report_agent"
    state["awaiting_human"] = False

    return state
