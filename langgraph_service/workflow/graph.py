from langgraph.graph import StateGraph, END
from schemas.state import AnalysisState

from agents.supervisor import supervisor_node
from agents.log_query_agent import log_query_agent_node
from agents.rag_agent import rag_agent_node
from agents.correlation_agent import correlation_agent_node
from agents.report_agent import report_agent_node
from internal.redis_client import save_analysis_state

builder = StateGraph(AnalysisState)

builder.add_node("supervisor", supervisor_node)
builder.add_node("log_query_agent", log_query_agent_node)
builder.add_node("rag_agent", rag_agent_node)
builder.add_node("correlation_agent", correlation_agent_node)
builder.add_node("report_agent", report_agent_node)

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

graph = builder.compile()

def get_graph():
    return graph

def run_analysis(state: dict) -> dict:
    """
    Invokes the graph on initial input and persists the final/paused state in Redis.
    """
    result = graph.invoke(state)
    analysis_id = result.get("analysis_id", "unknown_analysis")
    save_analysis_state(analysis_id, result)
    return result

def resume_analysis(state: dict) -> dict:
    """
    Prepares state to resume running and re-invokes the graph, updating persistence in Redis.
    """
    state["resume_count"] = state.get("resume_count", 0) + 1

    if state["resume_count"] > 2:
        state["status"] = "failed"
        state["awaiting_human"] = False
        state["waiting_at"] = None
        state["interrupt_type"] = None
        state["interrupt_question"] = None
        analysis_id = state.get("analysis_id", "unknown_analysis")
        save_analysis_state(analysis_id, state)
        return state

    state["status"] = "running"
    state["awaiting_human"] = False
    state["waiting_at"] = None
    state["interrupt_type"] = None
    state["interrupt_question"] = None

    result = graph.invoke(state)
    analysis_id = result.get("analysis_id", "unknown_analysis")
    save_analysis_state(analysis_id, result)
    return result