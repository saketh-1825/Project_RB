from langgraph.graph import StateGraph, END
from schemas.state import AnalysisState

from agents.supervisor import supervisor_node
from agents.log_query_agent import log_query_agent_node
from agents.rag_agent import rag_agent_node

builder = StateGraph(AnalysisState)

builder.add_node("supervisor", supervisor_node)
builder.add_node("log_query_agent", log_query_agent_node)
builder.add_node("rag_agent", rag_agent_node)

builder.set_entry_point("supervisor")

builder.add_edge("supervisor", "log_query_agent")
builder.add_edge("log_query_agent", "rag_agent")
builder.add_edge("rag_agent", END)

graph = builder.compile()

def get_graph():
    return graph