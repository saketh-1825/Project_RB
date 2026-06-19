from langgraph.graph import StateGraph, END
from schemas.state import AnalysisState
from agents.supervisor import supervisor_node

def log_query_agent_node(state: AnalysisState) -> AnalysisState:
    # Placeholder
    return state

def build_graph():
    builder = StateGraph(AnalysisState)

    builder.add_node("supervisor", supervisor_node)
    builder.add_node("log_query_agent", log_query_agent_node)

    builder.set_entry_point("supervisor")

    def route_supervisor(state: AnalysisState) -> str:
        if state.get("status") == "failed":
            return END
        return "log_query_agent"

    builder.add_conditional_edges(
        "supervisor",
        route_supervisor
    )
    
    # Just tying the placeholder to the END for graph compilation validity
    builder.add_edge("log_query_agent", END)

    return builder.compile()