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

    # 1. Build/Read rag_query using incident_title, incident_summary, and latest log finding
    query_parts = []
    incident_title = state.get("incident_title", "")
    incident_summary = state.get("incident_summary", "")

    if incident_title:
        query_parts.append(incident_title)
    if incident_summary:
        query_parts.append(incident_summary)

    findings = state.get("findings", [])
    if findings:
        log_findings = [f for f in findings if f.get("agent") == "log_query_agent"]
        if log_findings:
            latest_log_finding = log_findings[-1]
            log_title = latest_log_finding.get("title", "")
            log_summary = latest_log_finding.get("summary", "")
            if log_title:
                query_parts.append(log_title)
            if log_summary:
                query_parts.append(log_summary)

    # Combine incident details with log findings
    query = " ".join(query_parts).strip()
    
    # Store the final rag_query back into state
    state["rag_query"] = query

    # 2. Call search_runbooks
    try:
        search_response = client.search_runbooks(query)
        if isinstance(search_response, list):
            runbooks = search_response
        elif isinstance(search_response, dict):
            runbooks = search_response.get("results", search_response.get("runbooks"))
            if not isinstance(runbooks, list):
                if "runbook_id" in search_response or "id" in search_response:
                    runbooks = [search_response]
                else:
                    runbooks = []
        else:
            runbooks = []
    except Exception as e:
        print(f"RAG search failed: {e}")
        runbooks = []

    # 3. Process top runbook
    if runbooks:
        top_match = runbooks[0]
        runbook_id = top_match.get("runbook_id", top_match.get("id", "UNKNOWN"))
        title = top_match.get("title", "Unknown Runbook")
        summary = top_match.get("summary") or top_match.get("content", "No summary available")
        similarity_score = top_match.get("similarity_score", top_match.get("score", 0.0))

        # Build production finding format
        finding = {
            "agent": "rag_agent",
            "type": "runbook",
            "runbook_id": runbook_id,
            "title": title,
            "summary": summary,
            "similarity_score": similarity_score
        }

        if "findings" not in state or state["findings"] is None:
            state["findings"] = []
        state["findings"].append(finding)

        # Build event format
        event = {
            "source": "rag_agent",
            "event_type": "runbook_match",
            "message": f"Matched {title}",
            "details": finding
        }
        if "incident_events" not in state or state["incident_events"] is None:
            state["incident_events"] = []
        state["incident_events"].append(event)

        # Post finding to backend if incident_id exists
        incident_id = state.get("incident_id")
        if incident_id:
            try:
                client.post_finding(incident_id, finding)
            except Exception:
                pass

    state["current_agent"] = "rag_agent"
    state["status"] = "completed"
    
    return state
