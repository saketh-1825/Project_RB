from datetime import datetime
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

        current_from = from_time
        retries = 0
        max_retries = 2

        while True:
            params: Dict[str, Any] = {
                "from": current_from,
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

            if response.status_code >= 400:
                retryable = False
                if response.status_code == 504:
                    retryable = True
                else:
                    try:
                        body = response.json()
                        if isinstance(body, dict):
                            err_code = body.get("error", {}).get("code")
                            if err_code == "LOG_QUERY_TIMEOUT":
                                retryable = True
                    except Exception:
                        pass

                if retryable and retries < max_retries:
                    retries += 1
                    try:
                        f_clean = current_from.replace("Z", "+00:00")
                        t_clean = to_time.replace("Z", "+00:00")
                        f_dt = datetime.fromisoformat(f_clean)
                        t_dt = datetime.fromisoformat(t_clean)
                        
                        duration = t_dt - f_dt
                        half_duration = duration / 2
                        new_f_dt = t_dt - half_duration
                        
                        new_f_str = new_f_dt.isoformat()
                        if "+00:00" in new_f_str:
                            new_f_str = new_f_str.replace("+00:00", "Z")
                        elif current_from.endswith("Z") and not new_f_str.endswith("Z"):
                            new_f_str += "Z"
                        current_from = new_f_str
                    except Exception:
                        pass
                    continue

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