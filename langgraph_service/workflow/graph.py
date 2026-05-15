from langgraph.graph import StateGraph

from schemas.state import AnalysisState


# SUPERVISOR
def supervisor_node(state):

    print("Supervisor running")

    state["current_agent"] = "supervisor"

    return state



# AGENT 1
def log_query_agent_node(state):

    print("Log agent running")
    state["current_agent"] = "log_query_agent"
    finding = {

        "agent": "log_query_agent",

        "type": "log_anomaly",

        "summary": "Error spike detected"
    }

    state["findings"].append(finding)

    return state



# AGENT 2
def rag_agent_node(state):

    print("RAG agent running")
    state["current_agent"] = "rag_agent"

    finding = {

        "agent": "rag_agent",

        "type": "runbook_match",

        "summary": "Found documentation"
    }

    state["findings"].append(finding)

    return state



# AGENT 3
def correlation_agent_node(state):

    print("Correlation agent running")
    state["current_agent"] = "correlation_agent"

    finding = {

        "agent": "correlation_agent",

        "type": "resource_issue",

        "summary": "CPU spike observed"
    }

    state["findings"].append(finding)

    return state



# FINAL AGENT
def report_agent_node(state):

    print("Report agent running")
    state["current_agent"] = "report_agent"

    state["status"] = "completed"

    return state




def build_graph():

    builder = StateGraph(AnalysisState)


    builder.add_node("supervisor", supervisor_node)

    builder.add_node("log_query_agent", log_query_agent_node)

    builder.add_node("rag_agent", rag_agent_node)

    builder.add_node("correlation_agent", correlation_agent_node)

    builder.add_node("report_agent", report_agent_node)


    builder.set_entry_point("supervisor")


    builder.add_edge("supervisor", "log_query_agent")

    builder.add_edge("log_query_agent", "rag_agent")

    builder.add_edge("rag_agent", "correlation_agent")

    builder.add_edge("correlation_agent", "report_agent")


    return builder.compile()