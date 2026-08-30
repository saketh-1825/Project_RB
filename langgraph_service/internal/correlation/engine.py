from datetime import datetime, timezone
from typing import Any


def find_spike_time(metric_series: dict[str, Any] | None) -> datetime | None:
    """
    Finds the first timestamp where a metric spikes.
    Thresholds are mapped dynamically based on metric names.
    """
    if not metric_series or not metric_series.get("data_points"):
        return None

    pts = []
    for pt in metric_series.get("data_points", []):
        ts_str = pt.get("timestamp")
        val = pt.get("value")
        if ts_str is None or val is None:
            continue
        try:
            clean_ts = ts_str.replace("Z", "+00:00")
            ts = datetime.fromisoformat(clean_ts)
            pts.append((ts, float(val)))
        except (ValueError, TypeError):
            continue

    if not pts:
        return None

    # Sort chronologically
    pts.sort(key=lambda x: x[0])

    metric_name = metric_series.get("metric_name", "").lower()

    # Determine threshold based on metric type
    if "error_rate" in metric_name:
        # Spike if error rate > 5% (0.05 ratio or 5.0 percent)
        for ts, val in pts:
            if val > 0.05 or val > 5.0:
                return ts
    elif "db_pool_waiting" in metric_name:
        # Spike if any waiting connections exist (> 0.0)
        for ts, val in pts:
            if val > 0.0:
                return ts
    elif "cpu" in metric_name:
        # Spike if cpu > 90% (0.90 ratio or 90.0 percent)
        for ts, val in pts:
            if val > 90.0 or (0.90 < val <= 1.0):
                return ts
    elif "memory" in metric_name:
        # Spike if memory > 90% (0.90 ratio or 90.0 percent)
        for ts, val in pts:
            if val > 90.0 or (0.90 < val <= 1.0):
                return ts
    else:
        # Default spike threshold
        for ts, val in pts:
            if val > 0.5:
                return ts

    return None


def infer_root_cause(
    metrics: dict[str, Any], affected_services: list[str] | None = None
) -> dict[str, Any]:
    """
    Infers the probable root cause of an incident based on metric rules.
    """
    error_rate_series = metrics.get("error_rate") or metrics.get("http_error_rate")
    cpu_series = metrics.get("cpu") or metrics.get("process_cpu_usage")
    memory_series = (
        metrics.get("memory")
        or metrics.get("process_memory_bytes")
        or metrics.get("process_memory_usage")
    )
    db_pool_waiting_series = metrics.get("db_pool_waiting") or metrics.get(
        "db_pool_waiting_connections"
    )

    err_spike = find_spike_time(error_rate_series)
    db_spike = find_spike_time(db_pool_waiting_series)

    services = affected_services or []

    # RULE 1: DB Exhaustion
    # Triggered if db_pool_waiting spikes BEFORE error_rate spikes
    if db_spike and err_spike and db_spike < err_spike:
        return {
            "type": "DB_EXHAUSTION",
            "description": "Database connection pool saturation caused request failures",
            "confidence": 0.92,
            "affected_services": services,
            "supporting_metrics": ["db_pool_waiting", "error_rate"],
        }

    # RULE 2: CPU Pressure
    max_cpu = 0.0
    if cpu_series and cpu_series.get("data_points"):
        max_cpu = max(float(pt.get("value", 0.0)) for pt in cpu_series["data_points"])

    cpu_exceeded = max_cpu > 90.0 or (0.90 < max_cpu <= 1.0)
    if cpu_exceeded and err_spike is not None:
        return {
            "type": "CPU_PRESSURE",
            "description": "High CPU utilization coinciding with request error spike",
            "confidence": 0.80,
            "affected_services": services,
            "supporting_metrics": ["cpu", "error_rate"],
        }

    # RULE 3: Memory Pressure
    max_mem = 0.0
    if memory_series and memory_series.get("data_points"):
        max_mem = max(
            float(pt.get("value", 0.0)) for pt in memory_series["data_points"]
        )

    mem_exceeded = max_mem > 90.0 or (0.90 < max_mem <= 1.0)
    if mem_exceeded and err_spike is not None:
        return {
            "type": "MEMORY_PRESSURE",
            "description": "High memory utilization coinciding with request error spike",
            "confidence": 0.80,
            "affected_services": services,
            "supporting_metrics": ["memory", "error_rate"],
        }

    # RULE 4: Default Fallback (Unknown)
    return {
        "type": "UNKNOWN",
        "description": "Unable to correlate metrics spike with a known signature",
        "confidence": 0.30,
        "affected_services": services,
        "supporting_metrics": [],
    }


def find_historical_matches(
    incidents: list[dict[str, Any]], current_services: list[str]
) -> list[dict[str, Any]]:
    """
    Identifies and sorts past incidents based on affected services overlap.
    """
    if not current_services:
        return []

    current_services_set = set(current_services)
    similar_past_incidents = []

    for past in incidents:
        if not isinstance(past, dict):
            continue
        past_services = set(past.get("affected_services", []))
        overlap = past_services & current_services_set
        if overlap:
            similarity = round(len(overlap) / max(len(current_services_set), 1), 2)
            similar_past_incidents.append(
                {
                    "incident_id": past.get("incident_id"),
                    "title": past.get("title"),
                    "similarity_score": similarity,
                    "resolution": past.get("root_cause_summary")
                    or past.get("resolution")
                    or "No resolution recorded",
                    "affected_services": past.get("affected_services", []),
                }
            )

    # Sort similar incidents by similarity score descending
    similar_past_incidents.sort(key=lambda x: x["similarity_score"], reverse=True)
    return similar_past_incidents


def build_correlation_finding(
    root_cause: dict[str, Any],
    metric_names: list[str] | None = None,
    time_range: dict[str, str] | None = None,
) -> dict[str, Any]:
    """
    Assembles a contract-compliant finding object.
    """
    if metric_names is None:
        metric_names = ["error_rate", "cpu", "memory", "db_pool_waiting"]

    evidence: dict[str, Any] = {"metric_names": metric_names}

    time_range_dict = None
    if time_range:
        time_range_dict = {
            "from": time_range.get("from") or time_range.get("from_time"),
            "to": time_range.get("to") or time_range.get("to_time"),
        }
        evidence["time_range"] = time_range_dict

    confidence = root_cause.get("confidence", 0.30)

    return {
        "agent": "correlation_agent",
        "type": "historical_correlation",
        "severity": "high",
        "title": f"Root Cause Correlation Analysis: {root_cause.get('type', 'UNKNOWN')}",
        "summary": root_cause.get("description", "Correlation analysis completed."),
        "confidence": confidence,
        "metric_names": metric_names,
        "time_range": time_range_dict,
        "evidence": evidence,
        "created_at": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
    }
