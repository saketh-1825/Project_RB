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


CONFIDENCE_THRESHOLD = 0.75

def calculate_confidence(state: AnalysisState, evidence: dict) -> dict:
    from internal.correlation.engine import find_spike_time

    score = 0.0
    reasons = []
    missing_evidence = []

    # 1. Logs Check (+0.3)
    log_status = evidence.get("metadata", {}).get("collection_status", {}).get("logs", "success")
    log_findings = evidence.get("logs", {}).get("findings", [])
    if not log_findings and state.get("findings"):
        log_findings = [f for f in state.get("findings", []) if f.get("agent") == "log_query_agent"]
    
    has_strong_logs = any(
        f.get("type") == "log_anomaly" 
        and not f.get("degraded") 
        and len(f.get("evidence", {}).get("log_ids", [])) > 0 
        for f in log_findings
    )
    if log_status == "success" and has_strong_logs:
        score += 0.3
        reasons.append("Strong log evidence found (+0.3)")
    else:
        missing_evidence.append("logs")
        reasons.append("No strong log anomalies detected")

    # 2. Metrics Check (+0.3)
    metrics_status = evidence.get("metadata", {}).get("collection_status", {}).get("metrics", "success")
    metrics_res = evidence.get("metrics") or {}
    metrics_data = {}
    metrics_query_failed = metrics_res.get("metrics_query_failed", False)
    
    if metrics_status == "success" and not metrics_query_failed:
        metrics_response = metrics_res.get("metrics_response") or {}
        series_list = metrics_response.get("series", []) if metrics_response else []
        metrics_data = {s.get("metric_name"): s for s in series_list if isinstance(s, dict)}
        for m_name in list(metrics_data.keys()):
            if m_name == "http_error_rate":
                metrics_data["error_rate"] = metrics_data[m_name]
            elif m_name == "process_cpu_usage":
                metrics_data["cpu"] = metrics_data[m_name]
            elif m_name == "process_memory_bytes":
                metrics_data["memory"] = metrics_data[m_name]
            elif m_name == "db_pool_waiting_connections":
                metrics_data["db_pool_waiting"] = metrics_data[m_name]

    has_metric_anomaly = False
    if not metrics_query_failed and metrics_data:
        # Check if root cause type is not UNKNOWN
        rc = state.get("root_cause") or {}
        if rc.get("type") and rc.get("type") != "UNKNOWN":
            has_metric_anomaly = True
        else:
            # Check individual metrics for spikes
            for series in metrics_data.values():
                if find_spike_time(series) is not None:
                    has_metric_anomaly = True
                    break

    if metrics_status == "success" and not metrics_query_failed and has_metric_anomaly:
        score += 0.3
        reasons.append("Metric anomaly detected (+0.3)")
    else:
        missing_evidence.append("metrics")
        reasons.append("No metric anomalies detected")

    # 3. RAG / Runbook Check (+0.2)
    rag_status = evidence.get("metadata", {}).get("collection_status", {}).get("rag", "success")
    rag_findings = evidence.get("rag", {}).get("findings", [])
    if not rag_findings and state.get("findings"):
        rag_findings = [f for f in state.get("findings", []) if f.get("agent") == "rag_agent"]

    has_similar_runbook = any(
        f.get("type") == "runbook" and f.get("similarity_score", 0.0) >= 0.7 for f in rag_findings
    )
    if rag_status == "success" and has_similar_runbook:
        score += 0.2
        reasons.append("Similar incident runbook matched from RAG (+0.2)")
    else:
        missing_evidence.append("rag")
        reasons.append("No similar runbooks matched above threshold from RAG")

    # 4. Topology Check (+0.2)
    topology_status = evidence.get("metadata", {}).get("collection_status", {}).get("topology", "success")
    topology = evidence.get("topology") or state.get("services_topology")
    if topology_status == "success" and topology and topology.get("services"):
        score += 0.2
        reasons.append("Topology dependency matched (+0.2)")
    else:
        missing_evidence.append("topology")
        reasons.append("Services topology lookup failed or empty")

    # 5. Human Context Boost (+0.3)
    if state.get("human_context"):
        score += 0.3
        reasons.append("Human context provided (+0.3 boost)")

    score = min(round(score, 2), 1.0)
    
    if score >= CONFIDENCE_THRESHOLD:
        level = "HIGH"
    elif score >= 0.4:
        level = "MEDIUM"
    else:
        level = "LOW"

    reason_str = "; ".join(reasons)
    if missing_evidence:
        reason_str += f" | Missing evidence: {', '.join(missing_evidence)}"

    return {
        "score": score,
        "level": level,
        "reason": reason_str,
        "missing_evidence": missing_evidence
    }


def correlation_agent_node(state: AnalysisState) -> AnalysisState:
    """
    Correlates collected evidence to perform root cause analysis, calculate
    confidence scoring, and evaluate risk without querying any backends if evidence is present.
    """
    # 0. Pass-through if correlation findings already exist (resuming execution)
    findings = state.get("findings", [])
    
    # Load prompt template as requested
    try:
        load_prompt("correlation_agent.txt")
    except Exception:
        pass

    evidence = state.get("evidence")
    
    # Fallback to legacy backend collection if evidence is completely missing
    # (for backward compatibility with existing tests)
    if not evidence:
        if findings:
            correlation_findings = [f for f in findings if f.get("agent") == "correlation_agent"]
            if correlation_findings:
                state["current_agent"] = "report_agent"
                return state

        base_url = os.environ.get("GO_BACKEND_URL", "http://mock-go-backend:8080/api/v1")
        token = os.environ.get("SRE_INTERNAL_TOKEN", "mock-token")
        client = GoBackendClient(base_url=base_url, token=token)

        incident_id = state.get("incident_id")
        alert = state.get("alert") or {}
        affected_services = alert.get("affected_services", [])

        time_window = state.get("time_window")
        from_time_str = None
        to_time_str = None
        if time_window and isinstance(time_window, dict):
            from_time_str = time_window.get("from") or time_window.get("from_time") or time_window.get("start")
            to_time_str = time_window.get("to") or time_window.get("to_time") or time_window.get("end")

        if not from_time_str or not to_time_str:
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
            state["time_window"] = {"from": from_time_str, "to": to_time_str}

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

        similar_past_incidents = []
        try:
            to_dt = datetime.fromisoformat(to_time_str.replace("Z", "+00:00"))
            from_30d_str = (to_dt - timedelta(days=30)).astimezone(timezone.utc).isoformat().replace("+00:00", "Z")
            incidents_resp = client.get_incidents(from_time=from_30d_str, to_time=to_time_str)
            past_incidents = incidents_resp.get("incidents", [])
            similar_past_incidents = find_historical_matches(past_incidents, affected_services)
        except Exception:
            pass

        if metrics_query_failed:
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
            series_list = metrics_response.get("series", []) if metrics_response else []
            metrics_data = {s.get("metric_name"): s for s in series_list if isinstance(s, dict)}
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

        state["metrics_data"] = metrics_data
        state["metrics_summary"] = metrics_summary
        state["similar_incidents"] = similar_past_incidents
        state["root_cause"] = root_cause
        state["correlation_finding"] = correlation_finding

        if "findings" not in state or not isinstance(state["findings"], list):
            state["findings"] = []
        state["findings"].append(correlation_finding)

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

        if incident_id:
            try:
                client.post_finding(incident_id, correlation_finding)
            except Exception:
                pass

        state["current_agent"] = "report_agent"
        return state

    # --- Reasoning layer with state["evidence"] (Day 16) ---
    alert = state.get("alert") or {}
    affected_services = alert.get("affected_services", [])
    incident_id = state.get("incident_id")
    time_window = state.get("time_window") or {}
    from_time_str = time_window.get("from")
    to_time_str = time_window.get("to")

    metrics_res = evidence.get("metrics") or {}
    metrics_query_failed = metrics_res.get("metrics_query_failed", False)
    metrics_response = metrics_res.get("metrics_response") or {}
    similar_past_incidents = metrics_res.get("similar_past_incidents") or []

    if metrics_query_failed:
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
        series_list = metrics_response.get("series", []) if metrics_response else []
        metrics_data = {s.get("metric_name"): s for s in series_list if isinstance(s, dict)}
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

    # Calculate deterministic confidence
    confidence = calculate_confidence(state, evidence)

    # Determine risk level
    severity = alert.get("severity", "medium").upper()
    if severity not in ["LOW", "MEDIUM", "HIGH", "CRITICAL"]:
        severity = "HIGH" if severity == "CRITICAL" else "MEDIUM"
    impact = f"Potential service disruption affecting {', '.join(affected_services)}" if affected_services else "Unknown service impact"

    # Category determination
    rc_type = root_cause.get("type", "UNKNOWN")
    if rc_type in ["CPU_PRESSURE", "MEMORY_PRESSURE"]:
        category = "infrastructure"
    elif rc_type == "DB_EXHAUSTION":
        category = "dependency"
    else:
        category = "unknown"

    # Store structured correlation results in state (Backward compatible + structured Day 16 schema)
    state["metrics_data"] = metrics_data
    state["metrics_summary"] = metrics_summary
    state["similar_incidents"] = similar_past_incidents
    state["root_cause"] = root_cause
    state["correlation_finding"] = correlation_finding

    # Ensure findings is populated
    if "findings" not in state or not isinstance(state["findings"], list):
        state["findings"] = []
    
    # Only append if not already present
    if not any(f.get("agent") == "correlation_agent" for f in state["findings"]):
        state["findings"].append(correlation_finding)

    topology = evidence.get("topology") or state.get("services_topology") or {}

    state["correlation"] = {
        "root_cause": {
            "service": affected_services[0] if affected_services else "unknown",
            "reason": root_cause.get("description", "Undetermined root cause"),
            "category": category,
            # Backward compatible keys
            "description": correlation_finding.get("summary", "Correlation analysis completed."),
            "affected_services": affected_services,
            "confidence": correlation_finding.get("confidence", 0.3),
        },
        "confidence": {
            "score": confidence["score"],
            "level": confidence["level"],
            "reason": confidence["reason"]
        },
        "supporting_evidence": {
            "logs": evidence.get("logs", {}).get("findings", []),
            "metrics": list(metrics_data.values()),
            "rag": evidence.get("rag", {}).get("findings", []),
            "topology": topology.get("services", []) if isinstance(topology, dict) else []
        },
        "risk_level": {
            "severity": severity,
            "impact": impact
        },
        "similar_past_incidents": similar_past_incidents # Backward compatible key
    }

    event = {
        "source": "correlation_agent",
        "event_type": "degraded" if metrics_query_failed else "historical_correlation",
        "message": f"Correlation analysis completed — confidence {confidence['score']}",
        "details": correlation_finding
    }
    if "incident_events" not in state or not isinstance(state["incident_events"], list):
        state["incident_events"] = []
    
    if not any(e.get("source") == "correlation_agent" for e in state["incident_events"]):
        state["incident_events"].append(event)

    # Post finding to backend if incident_id is available (using backend client)
    if incident_id:
        base_url = os.environ.get("GO_BACKEND_URL", "http://mock-go-backend:8080/api/v1")
        token = os.environ.get("SRE_INTERNAL_TOKEN", "mock-token")
        client = GoBackendClient(base_url=base_url, token=token)
        try:
            client.post_finding(incident_id, correlation_finding)
        except Exception:
            pass
        finally:
            client.close()

    state["current_agent"] = "report_agent"
    return state
