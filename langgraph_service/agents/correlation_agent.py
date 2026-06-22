import os
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Optional

from schemas.state import AnalysisState
from internal.client.go_backend import GoBackendClient
from internal.errors import GoBackendError
from prompts import load_prompt
from internal.correlation.engine import (
    infer_root_cause,
    find_historical_matches,
    build_correlation_finding
)


def _get_metric_stats(series: Optional[Dict[str, Any]], scale_to_percentage: bool = False) -> Dict[str, Any]:
    if not series or not series.get("data_points"):
        return {"max": 0, "avg": 0}
    vals = [float(pt["value"]) for pt in series["data_points"] if pt.get("value") is not None]
    if not vals:
        return {"max": 0, "avg": 0}

    max_v = max(vals)
    scale = 100.0 if (scale_to_percentage and max_v <= 1.0) else 1.0

    scaled_vals = [v * scale for v in vals]
    return {
        "max": round(max(scaled_vals)),
        "avg": round(sum(scaled_vals) / len(scaled_vals))
    }


def correlation_agent_node(state: AnalysisState) -> AnalysisState:
    """
    Correlates findings from log_query_agent and rag_agent with
    metrics and historical incidents to identify probable root cause.
    """
    # 0. Pass-through if correlation findings already exist (resuming execution)
    findings = state.get("findings", [])
    if findings:
        correlation_findings = [f for f in findings if f.get("agent") == "correlation_agent"]
        if correlation_findings:
            state["current_agent"] = "report_agent"
            return state

    # Load prompt template as requested
    try:
        load_prompt("correlation_agent.txt")
    except Exception:
        pass

    base_url = os.environ.get("GO_BACKEND_URL", "http://mock-go-backend:8080/api/v1")
    token = os.environ.get("SRE_INTERNAL_TOKEN", "mock-token")
    client = GoBackendClient(base_url=base_url, token=token)

    # 1. Read input parameters from state
    incident_id = state.get("incident_id")
    alert = state.get("alert") or {}
    affected_services = alert.get("affected_services", [])

    # Extract time window from state or calculate it
    time_window = state.get("time_window")
    from_time_str = None
    to_time_str = None
    if time_window and isinstance(time_window, dict):
        from_time_str = time_window.get("from") or time_window.get("from_time") or time_window.get("start")
        to_time_str = time_window.get("to") or time_window.get("to_time") or time_window.get("end")

    if not from_time_str or not to_time_str:
        # Fallback to computing time window from alert fired_at
        fired_at_str = alert.get("fired_at")
        if fired_at_str:
            clean_time_str = fired_at_str.replace("Z", "+00:00")
            try:
                fired_at = datetime.fromisoformat(clean_time_str)
                if fired_at.tzinfo is None:
                    fired_at = fired_at.replace(tzinfo=timezone.utc)
            except ValueError:
                fired_at = datetime.now(timezone.utc)
        else:
            fired_at = datetime.now(timezone.utc)

        from_time_str = (fired_at - timedelta(minutes=10)).astimezone(timezone.utc).isoformat().replace("+00:00", "Z")
        to_time_str = fired_at.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")

        # Save generated time window in state
        state["time_window"] = {"from": from_time_str, "to": to_time_str}

    # 2. Call POST /metrics/query/batch
    metrics_query_failed = False
    metrics_response = {}
    try:
        queries = [
            {"metric_name": "error_rate", "from": from_time_str, "to": to_time_str},
            {"metric_name": "cpu", "from": from_time_str, "to": to_time_str},
            {"metric_name": "memory", "from": from_time_str, "to": to_time_str},
            {"metric_name": "db_pool_waiting", "from": from_time_str, "to": to_time_str}
        ]
        metrics_response = client.query_metrics_batch(queries)
    except Exception:
        metrics_query_failed = True

    # 3. Call GET /incidents for the previous 30 days
    similar_past_incidents = []
    try:
        to_dt = datetime.fromisoformat(to_time_str.replace("Z", "+00:00"))
        from_30d_str = (to_dt - timedelta(days=30)).astimezone(timezone.utc).isoformat().replace("+00:00", "Z")

        incidents_resp = client.get_incidents(from_time=from_30d_str, to_time=to_time_str)
        past_incidents = incidents_resp.get("incidents", [])

        # Match using the correlation engine
        similar_past_incidents = find_historical_matches(past_incidents, affected_services)
    except Exception:
        pass

    # 4. Ingest and calculate root cause or handle degraded mode
    if metrics_query_failed:
        # Create degraded finding as specified
        correlation_finding = {
            "agent": "correlation_agent",
            "type": "historical_correlation",
            "severity": "high",
            "title": "Root Cause Correlation Analysis: DEGRADED",
            "summary": "Metrics endpoint unavailable",
            "confidence": 0.2,
            "reason": "METRIC_QUERY_FAILED",
            "evidence": {
                "metric_names": ["error_rate", "cpu", "memory", "db_pool_waiting"],
                "time_range": {"from": from_time_str, "to": to_time_str}
            },
            "created_at": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")
        }

        root_cause = {
            "type": "UNKNOWN",
            "description": "Metrics query failed",
            "confidence": 0.2,
            "affected_services": affected_services,
            "supporting_metrics": []
        }
        metrics_data = {}
        metrics_summary = {}
    else:
        # Process metrics using the correlation engine
        series_list = metrics_response.get("series", []) if metrics_response else []
        metrics_data = {s.get("metric_name"): s for s in series_list if isinstance(s, dict)}

        # Also support full contract name mapping
        for m_name in list(metrics_data.keys()):
            if m_name == "http_error_rate":
                metrics_data["error_rate"] = metrics_data[m_name]
            elif m_name == "process_cpu_usage":
                metrics_data["cpu"] = metrics_data[m_name]
            elif m_name == "process_memory_bytes":
                metrics_data["memory"] = metrics_data[m_name]
            elif m_name == "db_pool_waiting_connections":
                metrics_data["db_pool_waiting"] = metrics_data[m_name]

        root_cause = infer_root_cause(metrics_data, affected_services)

        # Calculate metrics summary
        error_rate_stats = _get_metric_stats(metrics_data.get("error_rate"), scale_to_percentage=True)
        metrics_summary = {
            "cpu": _get_metric_stats(metrics_data.get("cpu"), scale_to_percentage=True),
            "memory": _get_metric_stats(metrics_data.get("memory"), scale_to_percentage=True),
            "error_rate": {"max": error_rate_stats["max"]}
        }

        time_range = {"from": from_time_str, "to": to_time_str}
        correlation_finding = build_correlation_finding(
            root_cause=root_cause,
            metric_names=["error_rate", "cpu", "memory", "db_pool_waiting"],
            time_range=time_range
        )

    # 5. Store in state
    state["metrics_data"] = metrics_data
    state["metrics_summary"] = metrics_summary
    state["similar_incidents"] = similar_past_incidents
    state["root_cause"] = root_cause
    state["correlation_finding"] = correlation_finding

    # Ensure findings list is populated
    if "findings" not in state or not isinstance(state["findings"], list):
        state["findings"] = []
    state["findings"].append(correlation_finding)

    # Maintain backward compatibility with report_agent.py
    state["correlation"] = {
        "root_cause": {
            "description": correlation_finding.get("summary", "Correlation analysis completed."),
            "affected_services": affected_services,
            "confidence": correlation_finding.get("confidence", 0.3),
        },
        "similar_past_incidents": similar_past_incidents
    }

    event = {
        "source": "correlation_agent",
        "event_type": "degraded" if metrics_query_failed else "historical_correlation",
        "message": f"Correlation analysis completed — confidence {correlation_finding.get('confidence', 0.3)}",
        "details": correlation_finding
    }
    if "incident_events" not in state or not isinstance(state["incident_events"], list):
        state["incident_events"] = []
    state["incident_events"].append(event)

    # 6. POST the finding to /incidents/{incident_id}/events
    if incident_id:
        try:
            client.post_finding(incident_id, correlation_finding)
        except Exception:
            pass

    # 7. Advance current agent
    state["current_agent"] = "report_agent"
    return state
