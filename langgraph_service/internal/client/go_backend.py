from typing import Any, Dict, List, Optional
import httpx

class GoBackendClient:
    def __init__(self, base_url: str, token: str):
        """
        Initializes the GoBackendClient.
        
        Args:
            base_url (str): The base URL for the Go backend API.
            token (str): The authorization token.
        """
        self.base_url = base_url.rstrip("/")
        self.client = httpx.Client(
            base_url=self.base_url,
            headers={"Authorization": f"Bearer {token}"}
        )

    def get_health(self) -> Dict[str, Any]:
        """
        Retrieves the health status of the backend.
        
        Returns:
            Dict[str, Any]: The JSON response containing health information.
        """
        response = self.client.get("/health")
        response.raise_for_status()
        return response.json()

    def get_services(self) -> Dict[str, Any]:
        """
        Retrieves the list of services.
        
        Returns:
            Dict[str, Any]: The JSON response containing services.
        """
        response = self.client.get("/services")
        response.raise_for_status()
        return response.json()

    def get_logs(
        self,
        from_time: str,
        to_time: str,
        services: Optional[List[str]] = None,
        levels: Optional[List[str]] = None
    ) -> Dict[str, Any]:
        """
        Retrieves logs within a specified time range, optionally filtered by services and levels.
        
        Args:
            from_time (str): The start time for the logs query.
            to_time (str): The end time for the logs query.
            services (Optional[List[str]]): A list of service names to filter by.
            levels (Optional[List[str]]): A list of log levels to filter by.
            
        Returns:
            Dict[str, Any]: The JSON response containing logs.
        """
        params: Dict[str, Any] = {
            "from": from_time,
            "to": to_time,
        }
        if services:
            params["services"] = ",".join(services)
        if levels:
            params["levels"] = ",".join(levels)
            
        response = self.client.get("/logs", params=params)
        response.raise_for_status()
        return response.json()

    def get_log_anomalies(
        self,
        from_time: str,
        to_time: str,
        services: Optional[List[str]] = None
    ) -> Dict[str, Any]:
        """
        Retrieves log anomalies within a specified time range.
        
        Args:
            from_time (str): The start time for the anomalies query.
            to_time (str): The end time for the anomalies query.
            services (Optional[List[str]]): A list of service names to filter by.
            
        Returns:
            Dict[str, Any]: The JSON response containing log anomalies.
        """
        params: Dict[str, Any] = {
            "from": from_time,
            "to": to_time,
        }
        if services:
            params["services"] = ",".join(services)
            
        response = self.client.get("/logs/anomalies", params=params)
        response.raise_for_status()
        return response.json()

    def get_trace(self, trace_id: str) -> Dict[str, Any]:
        """
        Retrieves the details of a specific trace.
        
        Args:
            trace_id (str): The ID of the trace.
            
        Returns:
            Dict[str, Any]: The JSON response containing trace details.
        """
        response = self.client.get(f"/traces/{trace_id}")
        response.raise_for_status()
        return response.json()

    def search_runbooks(self, query: str, top_k: int = 5) -> Dict[str, Any]:
        """
        Searches runbooks based on a query string.
        
        Args:
            query (str): The search query.
            top_k (int): The maximum number of results to return.
            
        Returns:
            Dict[str, Any]: The JSON response containing runbook search results.
        """
        params = {
            "q": query,
            "top_k": top_k
        }
        response = self.client.get("/runbooks/search", params=params)
        response.raise_for_status()
        return response.json()

    def create_incident(self, data: Dict[str, Any]) -> Dict[str, Any]:
        """
        Creates a new incident.
        
        Args:
            data (Dict[str, Any]): The incident data payload.
            
        Returns:
            Dict[str, Any]: The JSON response containing the created incident details.
        """
        response = self.client.post("/incidents", json=data)
        response.raise_for_status()
        return response.json()

    def post_finding(self, incident_id: str, finding: Dict[str, Any]) -> Dict[str, Any]:
        """
        Posts a finding/event to a specific incident.
        
        Args:
            incident_id (str): The ID of the incident.
            finding (Dict[str, Any]): The finding data payload.
            
        Returns:
            Dict[str, Any]: The JSON response confirming the finding was posted.
        """
        response = self.client.post(f"/incidents/{incident_id}/events", json=finding)
        response.raise_for_status()
        return response.json()

    def submit_report(self, incident_id: str, report: Dict[str, Any]) -> Dict[str, Any]:
        """
        Submits a report for a specific incident.
        
        Args:
            incident_id (str): The ID of the incident.
            report (Dict[str, Any]): The report data payload.
            
        Returns:
            Dict[str, Any]: The JSON response confirming the report was submitted.
        """
        response = self.client.post(f"/incidents/{incident_id}/report", json=report)
        response.raise_for_status()
        return response.json()
