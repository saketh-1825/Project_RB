import os
from typing import Any, Dict, List

from schemas.state import AnalysisState
from internal.client.go_backend import GoBackendClient

def build_search_query(title: str, summary: str) -> str:
    return f"{title} {summary}"

def build_runbook_finding(runbook_id: str, similarity_score: float, summary: str) -> Dict[str, Any]:
    return {
        "agent": "rag_agent",
        "type": "runbook_match",
        "severity": "medium",
        "title": "Matching runbook found",
        "summary": summary,
        "confidence": similarity_score,
        "evidence": {
            "runbook_id": runbook_id
        }
    }

def rag_agent_node(state: AnalysisState) -> AnalysisState:
    base_url = os.environ.get("GO_BACKEND_URL", "http://mock-go-backend:8080/api/v1")
    token = os.environ.get("SRE_INTERNAL_TOKEN", "mock-token")
    client = GoBackendClient(base_url=base_url, token=token)

    findings = state.get("findings", [])
    if not findings:
        state["current_agent"] = "correlation_agent"
        state["status"] = "running"
        return state

    latest_finding = findings[-1]
    finding_title = latest_finding.get("title", "")
    finding_summary = latest_finding.get("summary", "")

    query = build_search_query(finding_title, finding_summary)

    try:
        search_response = client.search_runbooks(query=query, top_k=5)
        if isinstance(search_response, list):
            runbooks = search_response
        else:
            runbooks = search_response.get("results", search_response.get("runbooks", []))
    except Exception:
        runbooks = []

    if runbooks:
        top_match = runbooks[0]
        runbook_id = top_match.get("runbook_id", top_match.get("id", ""))
        title = top_match.get("title", "")
        content = top_match.get("content", "")
        similarity_score = top_match.get("similarity_score", top_match.get("score", 0.0))
        
        summary_text = "Found runbook for this issue"
        if similarity_score < 0.7:
            summary_text += " Runbook match is uncertain."

        finding = build_runbook_finding(
            runbook_id=runbook_id,
            similarity_score=similarity_score,
            summary=summary_text
        )

        state["findings"].append(finding)

        incident_id = state.get("incident_id")
        if incident_id:
            try:
                client.post_finding(incident_id, finding)
            except Exception:
                pass

    state["current_agent"] = "correlation_agent"
    state["status"] = "running"
    
    return state
