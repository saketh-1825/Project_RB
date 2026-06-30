import os
import logging
from langgraph.graph import StateGraph, END
from schemas.state import AnalysisState

from agents.supervisor import supervisor_node
from agents.log_query_agent import log_query_agent_node
from agents.rag_agent import rag_agent_node
from agents.correlation_agent import correlation_agent_node
from agents.report_agent import report_agent_node
from internal.redis_client import save_analysis_state
from internal.client.go_backend import GoBackendClient

logger = logging.getLogger(__name__)

def notify_transition(agent_name: str, state: AnalysisState):
    incident_id = state.get("incident_id")
    if not incident_id:
        return
    
    base_url = os.environ.get("GO_BACKEND_URL", "http://mock-go-backend:8080/api/v1")
    token = os.environ.get("SRE_INTERNAL_TOKEN", "mock-token")
    client = GoBackendClient(base_url=base_url, token=token)
    try:
        client.patch_incident(incident_id, {"analysis": {"agent_switched": agent_name}})
    except Exception as e:
        logger.error(f"Transition notification failed: {e}")

def wrap_node(agent_name: str, node_func):
    def wrapper(state: AnalysisState):
        notify_transition(agent_name, state)
        return node_func(state)
    return wrapper

builder = StateGraph(AnalysisState)

builder.add_node("supervisor", wrap_node("supervisor", supervisor_node))
builder.add_node("log_query_agent", wrap_node("log_query_agent", log_query_agent_node))
builder.add_node("rag_agent", wrap_node("rag_agent", rag_agent_node))
builder.add_node("correlation_agent", wrap_node("correlation_agent", correlation_agent_node))
builder.add_node("report_agent", wrap_node("report_agent", report_agent_node))

# Intentionally mirrors the linear workflow to explicitly map a waiting node to its predecessor
PREVIOUS_NODE = {
    "log_query_agent": "supervisor",
    "rag_agent": "log_query_agent",
    "correlation_agent": "rag_agent",
    "report_agent": "correlation_agent"
}

builder.set_entry_point("supervisor")

builder.add_edge("supervisor", "log_query_agent")
builder.add_edge("log_query_agent", "rag_agent")

def route_after_rag(state: AnalysisState):
    if state.get("status") == "awaiting_human":
        return END
    return "correlation_agent"

builder.add_conditional_edges("rag_agent", route_after_rag)
builder.add_edge("correlation_agent", "report_agent")
builder.add_edge("report_agent", END)

from langgraph.checkpoint.memory import MemorySaver

memory = MemorySaver()
graph_with_checkpoint = builder.compile(checkpointer=memory)
graph_no_checkpoint = builder.compile()

def get_graph():
    return graph_no_checkpoint

def run_analysis(state: dict) -> dict:
    """
    Invokes the graph on initial input and persists the final/paused state in Redis.
    """
    analysis_id = state.get("analysis_id", "unknown_analysis")
    config = {"configurable": {"thread_id": analysis_id}}
    result = graph_with_checkpoint.invoke(state, config=config)
    save_analysis_state(analysis_id, result)
    return result

def resume_analysis(state: dict) -> dict:
    """
    Prepares state to resume running and re-invokes the graph, updating persistence in Redis.
    """
    state["resume_count"] = state.get("resume_count", 0) + 1
    analysis_id = state.get("analysis_id", "unknown_analysis")
    config = {"configurable": {"thread_id": analysis_id}}

    if state["resume_count"] > 2:
        state["status"] = "failed"
        state["awaiting_human"] = False
        state["waiting_at"] = None
        state["interrupt_type"] = None
        state["interrupt_question"] = None
        save_analysis_state(analysis_id, state)
        return state

    waiting_at = state.get("waiting_at")

    state["status"] = "running"
    state["awaiting_human"] = False
    state["waiting_at"] = None
    state["interrupt_type"] = None
    state["interrupt_question"] = None

    if waiting_at:
        as_node = PREVIOUS_NODE.get(waiting_at, "supervisor")
        graph_with_checkpoint.update_state(config, state, as_node=as_node)
        result = graph_with_checkpoint.invoke(None, config=config)
    else:
        result = graph_with_checkpoint.invoke(state, config=config)

    save_analysis_state(analysis_id, result)
    return result