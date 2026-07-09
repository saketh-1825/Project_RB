from datetime import datetime, timezone
import os
from internal.client.go_backend import GoBackendClient
from internal.errors import GoBackendError

def build_degraded_finding(
    agent: str,
    status_code: int,
    message: str,
    error_category: str
) -> dict:
    """
    Builds a degraded finding object when a dependency service call fails.
    """
    return {
        "agent": agent,
        "type": "degraded",
        "degraded": True,
        "severity": "medium",
        "title": "Backend unavailable",
        "summary": message,
        "confidence": 0.2,
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "error_category": error_category,
        "evidence": {
            "backend": "mock-go-backend",
            "status_code": status_code
        }
    }


def collect_topology_data() -> dict:
    """
    Fetches the services topology from the Go backend.
    """
    base_url = os.environ.get("GO_BACKEND_URL", "http://mock-go-backend:8080/api/v1")
    token = os.environ.get("SRE_INTERNAL_TOKEN", "mock-token")
    client = GoBackendClient(base_url=base_url, token=token)
    try:
        return client.get_services()
    except GoBackendError as e:
        raise e
    finally:
        client.close()


def collect_metrics_data(alert: dict, time_window: dict = None) -> dict:
    """
    Queries metrics and past incidents from the Go backend.
    """
    from datetime import datetime, timedelta, timezone
    from internal.client.go_backend import GoBackendClient
    from internal.correlation.engine import find_historical_matches

    base_url = os.environ.get("GO_BACKEND_URL", "http://mock-go-backend:8080/api/v1")
    token = os.environ.get("SRE_INTERNAL_TOKEN", "mock-token")
    client = GoBackendClient(base_url=base_url, token=token)

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
    affected_services = alert.get("affected_services", [])
    try:
        to_dt = datetime.fromisoformat(to_time_str.replace("Z", "+00:00"))
        from_30d_str = (to_dt - timedelta(days=30)).astimezone(timezone.utc).isoformat().replace("+00:00", "Z")
        incidents_resp = client.get_incidents(from_time=from_30d_str, to_time=to_time_str)
        past_incidents = incidents_resp.get("incidents", [])
        similar_past_incidents = find_historical_matches(past_incidents, affected_services)
    except Exception:
        pass

    client.close()

    return {
        "metrics_query_failed": metrics_query_failed,
        "metrics_response": metrics_response,
        "similar_past_incidents": similar_past_incidents,
        "time_window": {"from": from_time_str, "to": to_time_str}
    }

