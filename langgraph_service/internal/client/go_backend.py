from datetime import datetime
from typing import Any, Dict, List, Optional
import httpx

from internal.errors import (
    GoBackendError,
    BackendTimeoutError,
    BackendUnavailableError,
    BackendNotFoundError
)


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

    def _handle_response(self, response: httpx.Response) -> httpx.Response:
        if response.status_code == 404:
            raise BackendNotFoundError(
                status_code=404,
                message=f"Backend resource not found (404) at {response.url}",
                original_exception=None
            )
        elif response.status_code == 500:
            raise BackendUnavailableError(
                status_code=500,
                message=f"Backend server error (500) at {response.url}",
                original_exception=None
            )
        elif response.status_code == 504:
            raise BackendTimeoutError(
                status_code=504,
                message=f"Backend gateway timeout (504) at {response.url}",
                original_exception=None
            )
        
        try:
            response.raise_for_status()
        except httpx.HTTPStatusError as e:
            raise GoBackendError(
                status_code=response.status_code,
                message=f"HTTP status error: {e}",
                original_exception=e
            ) from e
        return response

    def _request(self, method: str, path: str, **kwargs) -> httpx.Response:
        try:
            response = self.client.request(method, path, **kwargs)
            return self._handle_response(response)
        except httpx.TimeoutException as e:
            raise BackendTimeoutError(
                status_code=504,
                message=f"Backend request timed out: {e}",
                original_exception=e
            ) from e
        except httpx.RequestError as e:
            raise BackendUnavailableError(
                status_code=503,
                message=f"Backend service unavailable: {e}",
                original_exception=e
            ) from e

    # ---------------------------------------------------
    # HEALTH
    # ---------------------------------------------------

    def get_health(self) -> Dict[str, Any]:
        response = self._request("GET", "/api/v1/health")
        return response.json()

    # ---------------------------------------------------
    # SERVICES
    # ---------------------------------------------------

    def get_services(self) -> Dict[str, Any]:
        response = self._request("GET", "/api/v1/services")
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

            try:
                response = self.client.get(
                    "/api/v1/logs",
                    params=params
                )
            except httpx.TimeoutException as e:
                if retries < max_retries:
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
                raise BackendTimeoutError(
                    status_code=504,
                    message=f"Request timeout: {e}",
                    original_exception=e
                ) from e
            except httpx.RequestError as e:
                raise BackendUnavailableError(
                    status_code=503,
                    message=f"Request failed: {e}",
                    original_exception=e
                ) from e

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

            self._handle_response(response)
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

        response = self._request(
            "GET",
            "/api/v1/logs/anomalies",
            params=params
        )
        return response.json()

    # ---------------------------------------------------
    # TRACES
    # ---------------------------------------------------

    def get_trace(self, trace_id: str) -> Dict[str, Any]:
        response = self._request(
            "GET",
            f"/api/v1/traces/{trace_id}"
        )
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

        response = self._request(
            "GET",
            "/api/v1/runbooks/search",
            params=params
        )
        return response.json()

    def get_runbooks(self) -> Dict[str, Any]:
        response = self._request("GET", "/api/v1/runbooks")
        return response.json()

    # ---------------------------------------------------
    # METRICS
    # ---------------------------------------------------

    def query_metrics_batch(
        self,
        queries: List[Dict[str, Any]]
    ) -> Dict[str, Any]:
        response = self._request(
            "POST",
            "/api/v1/metrics/query/batch",
            json={"queries": queries}
        )
        return response.json()

    # ---------------------------------------------------
    # INCIDENTS
    # ---------------------------------------------------

    def get_incidents(
        self,
        from_time: Optional[str] = None,
        to_time: Optional[str] = None,
        service: Optional[str] = None,
        severity: Optional[str] = None,
        status: Optional[str] = None,
        page: int = 1,
        page_size: int = 20
    ) -> Dict[str, Any]:
        params: Dict[str, Any] = {
            "page": page,
            "page_size": page_size
        }
        if from_time:
            params["from"] = from_time
        if to_time:
            params["to"] = to_time
        if service:
            params["service"] = service
        if severity:
            params["severity"] = severity
        if status:
            params["status"] = status

        response = self._request("GET", "/api/v1/incidents", params=params)
        return response.json()

    def create_incident(
        self,
        data: Dict[str, Any]
    ) -> Dict[str, Any]:

        response = self._request(
            "POST",
            "/api/v1/incidents",
            json=data
        )
        return response.json()

    def get_incident(
        self,
        incident_id: str
    ) -> Dict[str, Any]:

        response = self._request(
            "GET",
            f"/api/v1/incidents/{incident_id}"
        )
        return response.json()

    def post_finding(
        self,
        incident_id: str,
        finding: Dict[str, Any]
    ) -> Dict[str, Any]:

        response = self._request(
            "POST",
            f"/api/v1/incidents/{incident_id}/events",
            json=finding
        )
        return response.json()

    def submit_report(
        self,
        incident_id: str,
        report: Dict[str, Any]
    ) -> Dict[str, Any]:

        response = self._request(
            "POST",
            f"/api/v1/incidents/{incident_id}/report",
            json=report
        )
        return response.json()

    # ---------------------------------------------------
    # CLEANUP
    # ---------------------------------------------------

    def close(self):
        self.client.close()