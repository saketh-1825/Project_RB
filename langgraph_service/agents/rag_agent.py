import os

from agents.helpers import build_degraded_finding
from internal.client.go_backend import GoBackendClient
from internal.errors import GoBackendError
from prompts import load_prompt
from schemas.state import AnalysisState


def rag_agent_node(state: AnalysisState) -> AnalysisState:
    base_url = os.environ.get("GO_BACKEND_URL", "http://mock-go-backend:8080/api/v1")
    token = os.environ.get("SRE_INTERNAL_TOKEN", "mock-token")
    client = GoBackendClient(base_url=base_url, token=token)

    # 1. Build/Read rag_query using template loaded from file
    incident_title = state.get("incident_title", "")
    incident_summary = state.get("incident_summary", "")

    log_title = ""
    log_summary = ""
    findings = state.get("findings", [])
    if findings:
        log_findings = [f for f in findings if f.get("agent") == "log_query_agent"]
        if log_findings:
            latest_log_finding = log_findings[-1]
            log_title = latest_log_finding.get("title", "")
            log_summary = latest_log_finding.get("summary", "")

    human_context = state.get("human_context") or ""

    # Clear human_context from state temporarily for the pause check
    if "human_context" in state:
        state["human_context"] = None

    # Load prompt and format query
    template = load_prompt("rag_query_prompt.txt")
    query = template.format(
        title=incident_title,
        summary=incident_summary,
        log_title=log_title,
        log_summary=log_summary,
        human_context=human_context,
    ).strip()

    state["rag_query"] = query

    # 2. Call search_runbooks with degradation fallback
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
    except GoBackendError as e:
        category = getattr(e, "error_category", "backend_unavailable")
        finding = build_degraded_finding(
            agent="rag_agent",
            status_code=e.status_code,
            message="Runbook lookup failed",
            error_category=category,
        )
        if "findings" not in state or state["findings"] is None:
            state["findings"] = []
        state["findings"].append(finding)

        event = {
            "source": "rag_agent",
            "event_type": "degraded",
            "message": "Runbook lookup unavailable",
            "details": finding,
        }
        if "incident_events" not in state or state["incident_events"] is None:
            state["incident_events"] = []
        state["incident_events"].append(event)

        state["current_agent"] = "correlation_agent"
        return state

    # Get similarity score of top match
    similarity_score = 0.0
    if runbooks:
        top_match = runbooks[0]
        similarity_score = top_match.get(
            "similarity_score", top_match.get("score", 0.0)
        )

    # Check if we should pause for human context (similarity < 0.7 and no human_context yet)
    if similarity_score < 0.7 and not state.get("human_context"):
        state["status"] = "awaiting_human"
        state["awaiting_human"] = True
        state["waiting_at"] = "rag_agent"
        state["interrupt_type"] = "provide_context"
        state["interrupt_question"] = load_prompt("interrupt_question_prompt.txt")
        return state

    # Restore human_context on success
    state["human_context"] = human_context

    # 3. Process top runbook
    if runbooks:
        top_match = runbooks[0]
        runbook_id = top_match.get("runbook_id", top_match.get("id", "UNKNOWN"))
        title = top_match.get("title", "Unknown Runbook")
        summary = top_match.get("summary") or top_match.get(
            "content", "No summary available"
        )
        similarity_score = top_match.get(
            "similarity_score", top_match.get("score", 0.0)
        )

        # Build production finding format
        finding = {
            "agent": "rag_agent",
            "type": "runbook",
            "runbook_id": runbook_id,
            "title": title,
            "summary": summary,
            "similarity_score": similarity_score,
        }

        if "findings" not in state or state["findings"] is None:
            state["findings"] = []
        state["findings"].append(finding)

        # Build event format
        event = {
            "source": "rag_agent",
            "event_type": "runbook_match",
            "message": f"Matched {title}",
            "details": finding,
        }
        if "incident_events" not in state or state["incident_events"] is None:
            state["incident_events"] = []
        state["incident_events"].append(event)

        # Post finding to backend if incident_id exists
        incident_id = state.get("incident_id")
        if incident_id:
            try:
                client.post_finding(incident_id, finding)
            except Exception:  # noqa: BLE001, S110
                pass

    state["current_agent"] = "correlation_agent"
    state["awaiting_human"] = False

    return state
