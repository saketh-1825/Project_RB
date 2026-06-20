from typing import Any, Dict, List, Optional
import httpx


class GoBackendClient:
    def __init__(self, base_url: str, token: str):
        self.base_url = base_url.rstrip("/")

        self.client = httpx.Client(
            base_url=self.base_url,
            headers={
                "Authorization": f"Bearer {token}"
            },
            timeout=30.0
        )

    # ---------------------------------------------------
    # HEALTH
    # ---------------------------------------------------

    def get_health(self) -> Dict[str, Any]:
        response = self.client.get("/api/v1/health")
        response.raise_for_status()
        return response.json()

    # ---------------------------------------------------
    # SERVICES
    # ---------------------------------------------------

    def get_services(self) -> Dict[str, Any]:
        response = self.client.get("/api/v1/services")
        response.raise_for_status()
        return response.json()

    # ---------------------------------------------------
    # LOGS
    # ---------------------------------------------------

    def get_logs(
        self,
        from_time: str,
        to_time: str,
        services: Optional[List[str]] = None,
        levels: Optional[List[str]] = None
    ) -> Dict[str, Any]:

        params: Dict[str, Any] = {
            "from": from_time,
            "to": to_time
        }

        if services:
            params["services"] = ",".join(services)

        if levels:
            params["levels"] = ",".join(levels)

        response = self.client.get(
            "/api/v1/logs",
            params=params
        )

        response.raise_for_status()
        return response.json()

    def get_log_anomalies(
        self,
        from_time: str,
        to_time: str,
        services: Optional[List[str]] = None
    ) -> Dict[str, Any]:

        params: Dict[str, Any] = {
            "from": from_time,
            "to": to_time
        }

        if services:
            params["services"] = ",".join(services)

        response = self.client.get(
            "/api/v1/logs/anomalies",
            params=params
        )

        response.raise_for_status()
        return response.json()

    # ---------------------------------------------------
    # TRACES
    # ---------------------------------------------------

    def get_trace(self, trace_id: str) -> Dict[str, Any]:

        response = self.client.get(
            f"/api/v1/traces/{trace_id}"
        )

        response.raise_for_status()
        return response.json()

    # ---------------------------------------------------
    # RUNBOOKS
    # ---------------------------------------------------

    def search_runbooks(
        self,
        query: str,
        top_k: int = 5
    ) -> Dict[str, Any]:

        params = {
            "q": query,
            "top_k": top_k
        }

        response = self.client.get(
            "/api/v1/runbooks/search",
            params=params
        )

        response.raise_for_status()
        return response.json()

    def get_runbooks(self) -> Dict[str, Any]:

        response = self.client.get(
            "/api/v1/runbooks"
        )

        response.raise_for_status()
        return response.json()

    # ---------------------------------------------------
    # INCIDENTS
    # ---------------------------------------------------

    def create_incident(
        self,
        data: Dict[str, Any]
    ) -> Dict[str, Any]:

        response = self.client.post(
            "/api/v1/incidents",
            json=data
        )

        response.raise_for_status()
        return response.json()

    def get_incident(
        self,
        incident_id: str
    ) -> Dict[str, Any]:

        response = self.client.get(
            f"/api/v1/incidents/{incident_id}"
        )

        response.raise_for_status()
        return response.json()

    def post_finding(
        self,
        incident_id: str,
        finding: Dict[str, Any]
    ) -> Dict[str, Any]:

        response = self.client.post(
            f"/api/v1/incidents/{incident_id}/events",
            json=finding
        )

        response.raise_for_status()
        return response.json()

    def submit_report(
        self,
        incident_id: str,
        report: Dict[str, Any]
    ) -> Dict[str, Any]:

        response = self.client.post(
            f"/api/v1/incidents/{incident_id}/report",
            json=report
        )

        response.raise_for_status()
        return response.json()

    # ---------------------------------------------------
    # CLEANUP
    # ---------------------------------------------------

    def close(self):
        self.client.close()