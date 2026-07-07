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

