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

def normalize_confidence(confidence: dict) -> dict:
    return {
        "score": confidence.get("score", 0.0),
        "level": confidence.get("level", "LOW"),
        "reason": confidence.get("reason", ""),
        "explanation": confidence.get("explanation", ""),
        "positive_factors": confidence.get("positive_factors", []),
        "negative_factors": confidence.get("negative_factors", [])
    }



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


def analyze_evidence_quality(evidence: dict, state: dict) -> dict:
    """
    Analyzes the quality of evidence deterministically.
    """
    available_sources = []
    missing_sources = []
    conflicts = []
    quality_score = 0.0
    
    if not evidence:
        return {
            "available_sources": [],
            "missing_sources": [{"source": "all", "impact": "No evidence provided"}],
            "conflicts": [],
            "quality_score": 0.0
        }
    
    metadata = evidence.get("metadata", {}).get("collection_status", {})
    
    # Check Logs
    log_status = metadata.get("logs", "success")
    log_findings = evidence.get("logs", {}).get("findings", [])
    if not log_findings and state.get("findings"):
        log_findings = [f for f in state.get("findings", []) if f.get("agent") == "log_query_agent"]
    
    has_logs = log_status == "success" and len(log_findings) > 0
    if has_logs:
        available_sources.append("logs")
        quality_score += 0.25
    else:
        missing_sources.append({"source": "logs", "impact": "Cannot verify application errors from logs"})

    # Check Metrics
    metrics_status = metadata.get("metrics", "success")
    metrics_res = evidence.get("metrics") or {}
    metrics_query_failed = metrics_res.get("metrics_query_failed", False)
    metrics_response = metrics_res.get("metrics_response") or {}
    series_list = metrics_response.get("series", []) if metrics_response else []
    
    has_metrics = metrics_status == "success" and not metrics_query_failed and len(series_list) > 0
    if has_metrics:
        available_sources.append("metrics")
        quality_score += 0.25
    else:
        missing_sources.append({"source": "metrics", "impact": "Cannot verify resource anomalies"})

    # Check RAG
    rag_status = metadata.get("rag", "success")
    rag_findings = evidence.get("rag", {}).get("findings", [])
    if not rag_findings and state.get("findings"):
        rag_findings = [f for f in state.get("findings", []) if f.get("agent") == "rag_agent"]
        
    has_rag = rag_status == "success" and len(rag_findings) > 0
    if has_rag:
        available_sources.append("rag")
        quality_score += 0.25
    else:
        missing_sources.append({"source": "rag", "impact": "No historical runbooks found"})

    # Check Topology
    topology_status = metadata.get("topology", "success")
    topology = evidence.get("topology") or state.get("services_topology")
    
    has_topology = topology_status == "success" and topology and topology.get("services")
    if has_topology:
        available_sources.append("topology")
        quality_score += 0.25
    else:
        missing_sources.append({"source": "topology", "impact": "Cannot verify service dependencies"})
        
    # Check Conflicts
    has_error_logs = any(f.get("type") == "log_anomaly" and not f.get("degraded") for f in log_findings)
    has_metric_anomaly = False
    
    from internal.correlation.engine import find_spike_time
    for s in series_list:
        if isinstance(s, dict) and find_spike_time(s) is not None:
            has_metric_anomaly = True
            break
            
    if has_error_logs and has_metrics and not has_metric_anomaly:
        conflicts.append({
            "type": "LOG_METRIC_MISMATCH",
            "description": "Logs indicate failures but metrics show normal state"
        })
        quality_score = max(0.0, quality_score - 0.2)
        
    return {
        "available_sources": available_sources,
        "missing_sources": missing_sources,
        "conflicts": conflicts,
        "quality_score": round(quality_score, 2)
    }

def calculate_risk(evidence: dict, root_cause: dict, affected_services: list, alert: dict) -> dict:
    """
    Calculates deterministic risk scoring based on evidence, root cause and affected services.
    """
    severity = alert.get("severity", "medium").upper()
    is_payment = any("payment" in s.lower() or "customer" in s.lower() for s in affected_services)
    
    # Collect metric max error rate if available
    metrics_res = evidence.get("metrics") or {}
    metrics_response = metrics_res.get("metrics_response") or {}
    series_list = metrics_response.get("series", []) if metrics_response else []
    error_rate = 0.0
    for s in series_list:
        if isinstance(s, dict) and s.get("metric_name") in ["error_rate", "http_error_rate"]:
            vals = [float(pt["value"]) for pt in s.get("data_points", []) if pt.get("value") is not None]
            if vals:
                error_rate = max(vals)
            break
            
    has_confirmed_rc = root_cause and root_cause.get("type") != "UNKNOWN" and root_cause.get("confidence", 0.0) >= 0.75
    
    if is_payment or severity == "CRITICAL" or error_rate > 0.5:
        return {
            "level": "CRITICAL",
            "reason": "Critical service affected (payment/customer-facing) or extremely high error rate",
            "affected_services": affected_services
        }
    elif has_confirmed_rc or severity == "HIGH":
        return {
            "level": "HIGH",
            "reason": "Confirmed root cause with production service degradation",
            "affected_services": affected_services
        }
    elif evidence.get("metrics", {}).get("metrics_query_failed") or not evidence:
        return {
            "level": "MEDIUM",
            "reason": "Partial evidence available or missing metrics",
            "affected_services": affected_services
        }
    else:
        return {
            "level": "LOW",
            "reason": "Weak signals only, no clear critical impact",
            "affected_services": affected_services
        }

CONFIDENCE_THRESHOLD = 0.75

def calculate_confidence(state: AnalysisState, evidence: dict) -> dict:
    from internal.correlation.engine import find_spike_time

    score = 0.0
    reasons = []
    positive_factors = []
    negative_factors = []
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
        msg = "Strong log evidence found (+0.3)"
        reasons.append(msg)
        positive_factors.append(msg)
    else:
        missing_evidence.append("logs")
        msg = "No strong log anomalies detected"
        reasons.append(msg)
        negative_factors.append(msg)

    # 2. Metrics Check (+0.3)
    metrics_status = evidence.get("metadata", {}).get("collection_status", {}).get("metrics", "success")
    metrics_res = evidence.get("metrics") or {}
    print("METRICS_RES:", metrics_res)
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
        msg = "Metric anomaly detected (+0.3)"
        reasons.append(msg)
        positive_factors.append(msg)
    else:
        missing_evidence.append("metrics")
        msg = "No metric anomalies detected"
        reasons.append(msg)
        negative_factors.append(msg)

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
        msg = "Similar incident runbook matched from RAG (+0.2)"
        reasons.append(msg)
        positive_factors.append(msg)
    else:
        missing_evidence.append("rag")
        msg = "No similar runbooks matched above threshold from RAG"
        reasons.append(msg)
        negative_factors.append(msg)

    # 4. Topology Check (+0.2)
    topology_status = evidence.get("metadata", {}).get("collection_status", {}).get("topology", "success")
    topology = evidence.get("topology") or state.get("services_topology")
    if topology_status == "success" and topology and topology.get("services"):
        score += 0.2
        msg = "Topology dependency matched (+0.2)"
        reasons.append(msg)
        positive_factors.append(msg)
    else:
        missing_evidence.append("topology")
        msg = "Services topology lookup failed or empty"
        reasons.append(msg)
        negative_factors.append(msg)

    # 5. Human Context Boost (+0.3)
    if state.get("human_context"):
        score += 0.3
        msg = "Human context provided (+0.3 boost)"
        reasons.append(msg)
        positive_factors.append(msg)

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
        
    explanation = f"{level} confidence ({score}) based on evidence."
    if positive_factors:
        explanation += f" Supporting: {', '.join(positive_factors)}."
    if negative_factors:
        explanation += f" Detracting: {', '.join(negative_factors)}."

    return {
        "score": score,
        "level": level,
        "reason": reason_str,
        "explanation": explanation,
        "positive_factors": positive_factors,
        "negative_factors": negative_factors,
        "missing_evidence": missing_evidence
    }


def correlation_agent_node(state: AnalysisState) -> AnalysisState:
    try:
        return _correlation_agent_node_impl(state)
    except Exception as e:
        import traceback
        traceback.print_exc()
        raise

def _correlation_agent_node_impl(state: AnalysisState) -> AnalysisState:
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

    # Store root_cause early so calculate_confidence can use it
    state["root_cause"] = root_cause
    
    # Calculate deterministic confidence
    confidence = calculate_confidence(state, evidence)
    print("CONFIDENCE TYPE:", type(confidence))

    # Determine risk level
    severity = alert.get("severity", "medium").upper()
    if severity not in ["LOW", "MEDIUM", "HIGH", "CRITICAL"]:
        severity = "HIGH" if severity == "CRITICAL" else "MEDIUM"
    impact = f"Potential service disruption affecting {', '.join(affected_services)}" if affected_services else "Unknown service impact"
    
    risk_assessment = calculate_risk(evidence, root_cause, affected_services, alert)
    evidence_quality = analyze_evidence_quality(evidence, state)

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
    state["evidence_quality"] = evidence_quality
    state["risk_assessment"] = risk_assessment

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
            "reasoning": confidence.get("explanation", ""),
            # Backward compatible keys
            "description": correlation_finding.get("summary", "Correlation analysis completed."),
            "affected_services": affected_services,
            "confidence": correlation_finding.get("confidence", 0.3),
        },
        "confidence": normalize_confidence(confidence),
        "evidence_quality": evidence_quality,
        "risk": risk_assessment,
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
        "details": correlation_finding,
        "data": {
            "risk_level": risk_assessment,
            "evidence_quality": evidence_quality
        }
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
