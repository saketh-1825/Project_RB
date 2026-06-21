from datetime import datetime, timezone

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
