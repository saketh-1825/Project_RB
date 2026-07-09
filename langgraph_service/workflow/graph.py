import os
import logging
from langgraph.graph import StateGraph, END
from schemas.state import AnalysisState

from agents.supervisor import supervisor_node
from agents.evidence_agent import evidence_agent_node
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

from contextvars import ContextVar
from internal.graph_events import emit_event

# Task-scoped tracking of executed nodes
executed_nodes_var: ContextVar[set] = ContextVar("executed_nodes")

TRACKED_NODES = [
    "supervisor",
    "analysis_coordinator",
    "evidence_agent",
    "correlation_agent",
    "human_review",
    "report_agent"
]

def wrap_node(agent_name: str, node_func_name: str):
    def wrapper(state: AnalysisState):
        notify_transition(agent_name, state)
        
        analysis_id = state.get("analysis_id", "unknown_analysis")
        
        # Track executed nodes
        try:
            executed_nodes_var.get().add(agent_name)
        except Exception:
            pass
            
        # 1. Emit Agent Switched (Running)
        try:
            if agent_name != "supervisor":
                emit_event(
                    analysis_id=analysis_id,
                    event_type="analysis.agent_switched",
                    node=agent_name,
                    status="running",
                    payload={"message": f"Agent {agent_name} started running", "data": {}}
                )
        except Exception as e:
            logger.error(f"Event emission failed: {e}")
            
        # Findings before node execution
        findings_before = list(state.get("findings", [])) if state.get("findings") else []
        
        try:
            # Resolve callable dynamically during execution to support testing/patching
            node_func = globals()[node_func_name]
            result = node_func(state)
            
            # Emit findings added during this node execution
            findings_after = result.get("findings", []) if result else []
            new_findings = [f for f in findings_after if f not in findings_before]
            
            for f in new_findings:
                try:
                    src = f.get("agent") or agent_name
                    if src == "log_query_agent":
                        src = "logs"
                    elif src == "rag_agent":
                        src = "rag"
                        
                    f_payload = {
                        "source": src,
                        "message": f.get("summary") or f.get("title") or "Discovery made"
                    }
                    
                    if agent_name == "correlation_agent":
                        rc = result.get("root_cause") or {}
                        f_payload = {
                            "root_cause": rc.get("type") or rc.get("description") or "unknown",
                            "confidence": rc.get("confidence") or f.get("confidence") or 0.0
                        }
                        
                    emit_event(
                        analysis_id=analysis_id,
                        event_type="analysis.finding",
                        node=agent_name,
                        status="completed",
                        payload=f_payload
                    )
                except Exception as ex:
                    logger.error(f"Finding event emission failed: {ex}")
                    
            # 2. Emit Agent Switched (Completed)
            try:
                payload_data = {}
                if agent_name == "correlation_agent" and result:
                    confidence_score = result.get("correlation", {}).get("confidence", {}).get("score", 0.0)
                    payload_data["confidence"] = confidence_score
                    
                emit_event(
                    analysis_id=analysis_id,
                    event_type="analysis.agent_switched",
                    node=agent_name,
                    status="completed",
                    payload={"message": f"Agent {agent_name} completed", "data": payload_data}
                )
            except Exception as e:
                logger.error(f"Event emission failed: {e}")
                
            return result
        except Exception as e:
            # 3. Emit Agent Switched (Failed)
            try:
                emit_event(
                    analysis_id=analysis_id,
                    event_type="analysis.agent_switched",
                    node=agent_name,
                    status="failed",
                    payload={"message": f"Agent {agent_name} failed", "error": str(e), "data": {}}
                )
            except Exception as ex:
                logger.error(f"Event emission failed: {ex}")
            raise e
    return wrapper

from internal.analysis_coordinator import detect_and_link_related_analyses
from agents.human_review_agent import human_review_node

builder = StateGraph(AnalysisState)

builder.add_node("supervisor", wrap_node("supervisor", "supervisor_node"))
builder.add_node("analysis_coordinator", wrap_node("analysis_coordinator", "detect_and_link_related_analyses"))
builder.add_node("evidence_agent", wrap_node("evidence_agent", "evidence_agent_node"))
builder.add_node("correlation_agent", wrap_node("correlation_agent", "correlation_agent_node"))
builder.add_node("report_agent", wrap_node("report_agent", "report_agent_node"))
builder.add_node("human_review", wrap_node("human_review", "human_review_node"))

# Intentionally mirrors the workflow to explicitly map a waiting node to its predecessor
PREVIOUS_NODE = {
    "evidence_agent": "supervisor",
    "rag_agent": "supervisor",
    "correlation_agent": "evidence_agent",
    "confidence_review": "evidence_agent",   # Resume from confidence_review runs correlation_agent next
    "report_agent": "correlation_agent"
}

builder.set_entry_point("supervisor")

# Route through Analysis Coordinator to detect overlapping alerts before evidence collection
builder.add_edge("supervisor", "analysis_coordinator")
builder.add_edge("analysis_coordinator", "evidence_agent")

def route_after_evidence(state: AnalysisState):
    if state.get("status") == "awaiting_human":
        return END
    return "correlation_agent"

def confidence_router(state: AnalysisState):
    """
    Pure routing function deciding between report generation and human review.
    """
    if state.get("backend_health") == "unavailable":
        return "report_agent"
    confidence = state.get("correlation", {}).get("confidence", {}).get("score", 0.0)
    if confidence >= 0.75:
        return "report_agent"
    return "human_review"

builder.add_conditional_edges("evidence_agent", route_after_evidence)
builder.add_conditional_edges("correlation_agent", confidence_router)
builder.add_edge("report_agent", END)
builder.add_edge("human_review", END)


from langgraph.checkpoint.memory import MemorySaver

memory = MemorySaver()
graph_with_checkpoint = builder.compile(checkpointer=memory)
graph_no_checkpoint = builder.compile()

def get_graph():
    return graph_no_checkpoint

def handle_run_completion(analysis_id: str, result: dict, executed: set):
    import json
    from internal.redis_client import _get_redis
    
    # 1. Determine and emit skipped nodes
    try:
        r = _get_redis()
        existing_events = r.lrange(f"analysis:{analysis_id}:events", 0, -1)
        executed_previously = set()
        for ev_str in existing_events:
            try:
                ev = json.loads(ev_str)
                if ev.get("event_type") == "analysis.agent_switched" and ev.get("status") in ["completed", "failed", "running"]:
                    executed_previously.add(ev.get("node"))
            except Exception:
                pass
                
        skipped_nodes = [n for n in TRACKED_NODES if n not in executed and n not in executed_previously]
        for node in skipped_nodes:
            emit_event(
                analysis_id=analysis_id,
                event_type="analysis.agent_switched",
                node=node,
                status="skipped",
                payload={"message": f"Agent {node} was skipped", "data": {}}
            )
    except Exception as e:
        logger.error(f"Failed to identify/emit skipped nodes: {e}")

    # 2. Emit final lifecycle events
    try:
        status = result.get("status")
        if status == "completed":
            emit_event(
                analysis_id=analysis_id,
                event_type="analysis.completed",
                node="report_agent",
                status="completed",
                payload={"message": "Analysis completed successfully", "data": {}}
            )
        elif status == "failed":
            emit_event(
                analysis_id=analysis_id,
                event_type="analysis.failed",
                node="report_agent",
                status="failed",
                payload={"message": "Analysis failed", "error": "Max resumptions exceeded", "data": {}}
            )
    except Exception as e:
        logger.error(f"Failed to emit final lifecycle event: {e}")

def run_analysis(state: dict) -> dict:
    """
    Invokes the graph on initial input and persists the final/paused state in Redis.
    """
    analysis_id = state.get("analysis_id", "unknown_analysis")
    config = {"configurable": {"thread_id": analysis_id}}
    
    token = executed_nodes_var.set(set())
    try:
        emit_event(
            analysis_id=analysis_id,
            event_type="analysis.started",
            node="supervisor",
            status="running",
            payload={"message": "Analysis started", "data": {}}
        )
    except Exception as e:
        logger.error(f"Event emission failed: {e}")
        
    try:
        result = graph_with_checkpoint.invoke(state, config=config)
        
        executed = executed_nodes_var.get()
        handle_run_completion(analysis_id, result, executed)
        
        save_analysis_state(analysis_id, result)
        return result
    except Exception as e:
        try:
            emit_event(
                analysis_id=analysis_id,
                event_type="analysis.failed",
                node="supervisor",
                status="failed",
                payload={"message": "Analysis run crashed", "error": str(e), "data": {}}
            )
        except Exception:
            pass
        raise e
    finally:
        executed_nodes_var.reset(token)

def resume_analysis(state: dict) -> dict:
    """
    Prepares state to resume running and re-invokes the graph, updating persistence in Redis.
    """
    state["resume_count"] = state.get("resume_count", 0) + 1
    analysis_id = state.get("analysis_id", "unknown_analysis")
    config = {"configurable": {"thread_id": analysis_id}}

    token = executed_nodes_var.set(set())
    try:
        emit_event(
            analysis_id=analysis_id,
            event_type="analysis.started",
            node="supervisor",
            status="running",
            payload={"message": "Analysis resumed", "data": {}}
        )
    except Exception as e:
        logger.error(f"Event emission failed: {e}")

    try:
        if state["resume_count"] > 2:
            state["status"] = "failed"
            state["awaiting_human"] = False
            state["waiting_at"] = None
            state["interrupt_type"] = None
            state["interrupt_question"] = None
            
            try:
                emit_event(
                    analysis_id=analysis_id,
                    event_type="analysis.failed",
                    node="report_agent",
                    status="failed",
                    payload={"message": "Analysis failed: Max resumptions exceeded", "error": "Exceeded limit of 2 resumptions", "data": {}}
                )
            except Exception:
                pass
                
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

        executed = executed_nodes_var.get()
        handle_run_completion(analysis_id, result, executed)

        save_analysis_state(analysis_id, result)
        return result
    except Exception as e:
        try:
            emit_event(
                analysis_id=analysis_id,
                event_type="analysis.failed",
                node="supervisor",
                status="failed",
                payload={"message": "Analysis resume crashed", "error": str(e), "data": {}}
            )
        except Exception:
            pass
        raise e
    finally:
        executed_nodes_var.reset(token)