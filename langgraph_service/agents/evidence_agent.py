import asyncio
import copy
import logging
from typing import Dict, Any

from schemas.state import AnalysisState
from agents.log_query_agent import log_query_agent_node
from agents.rag_agent import rag_agent_node
from agents.correlation_agent import correlation_agent_node
from agents.helpers import collect_topology_data
from concurrent.futures import ThreadPoolExecutor

logger = logging.getLogger(__name__)

# Reusable helper to run async coroutines from a synchronous context safely
def run_async(coro):
    with ThreadPoolExecutor(max_workers=1) as executor:
        return executor.submit(asyncio.run, coro).result()

# Synchronous wrappers for running the black-box agent nodes on deep-copied states
# in thread pools via asyncio.to_thread.
async def collect_logs(state: AnalysisState) -> AnalysisState:
    state_copy = copy.deepcopy(state)
    return await asyncio.to_thread(log_query_agent_node, state_copy)

async def collect_rag(state: AnalysisState) -> AnalysisState:
    state_copy = copy.deepcopy(state)
    return await asyncio.to_thread(rag_agent_node, state_copy)

async def collect_metrics(state: AnalysisState) -> AnalysisState:
    state_copy = copy.deepcopy(state)
    return await asyncio.to_thread(correlation_agent_node, state_copy)

async def collect_topology(state: AnalysisState) -> Dict[str, Any]:
    if state.get("services_topology") is not None:
        return state.get("services_topology")
    return await asyncio.to_thread(collect_topology_data)

async def orchestrate_evidence(state: AnalysisState) -> tuple:
    # Execute collectors in parallel using asyncio.gather
    return await asyncio.gather(
        collect_logs(state),
        collect_metrics(state),
        collect_rag(state),
        collect_topology(state),
        return_exceptions=True
    )

def evidence_agent_node(state: AnalysisState) -> AnalysisState:
    """
    Coordinates and executes evidence collection from existing agents in parallel,
    building a unified evidence object and maintaining backward compatibility.
    """
    logger.info("Executing Evidence Orchestration Layer...")
    
    # 1. Run independent collectors concurrently
    results = run_async(orchestrate_evidence(state))

    log_res = results[0]
    metrics_res = results[1]
    rag_res = results[2]
    topology_res = results[3]

    # Initialize unified evidence object
    evidence = {
        "logs": {},
        "metrics": {},
        "rag": {},
        "topology": {},
        "metadata": {
            "collection_status": {},
            "errors": []
        }
    }

    # Initialize collections in main state if not present
    if "findings" not in state or state["findings"] is None:
        state["findings"] = []
    if "incident_events" not in state or state["incident_events"] is None:
        state["incident_events"] = []

    # Helper to check for errors
    def handle_result(res, name: str, agent_name: str = None):
        if isinstance(res, Exception):
            evidence["metadata"]["collection_status"][name] = "failed"
            err_msg = f"{name} collection failed: {str(res)}"
            logger.error(err_msg)
            evidence["metadata"]["errors"].append(err_msg)
            return False
        
        if agent_name and isinstance(res, dict):
            findings = res.get("findings", [])
            for f in findings:
                if f.get("agent") == agent_name and f.get("type") == "degraded":
                    evidence["metadata"]["collection_status"][name] = "failed"
                    err_msg = f"{name} collection failed: {f.get('summary', 'Unknown error')}"
                    logger.error(err_msg)
                    evidence["metadata"]["errors"].append(err_msg)
                    return False

        evidence["metadata"]["collection_status"][name] = "success"
        return True

    # 1. LOGS
    if handle_result(log_res, "logs", "log_query_agent"):
        # Extract findings and events belonging to log_query_agent
        log_findings = [f for f in log_res.get("findings", []) if f.get("agent") == "log_query_agent"]
        log_events = [e for e in log_res.get("incident_events", []) if e.get("source") == "log_query_agent"]
        evidence["logs"] = {
            "findings": log_findings,
            "incident_events": log_events
        }
        # Merge back to primary state for backward compatibility
        for f in log_findings:
            if f not in state["findings"]:
                state["findings"].append(f)
        for e in log_events:
            if e not in state["incident_events"]:
                state["incident_events"].append(e)

    # 2. RAG
    if handle_result(rag_res, "rag", "rag_agent"):
        rag_findings = [f for f in rag_res.get("findings", []) if f.get("agent") == "rag_agent"]
        rag_events = [e for e in rag_res.get("incident_events", []) if e.get("source") == "rag_agent"]
        evidence["rag"] = {
            "findings": rag_findings,
            "incident_events": rag_events,
            "rag_query": rag_res.get("rag_query")
        }
        # Merge back to primary state for backward compatibility
        state["rag_query"] = rag_res.get("rag_query")
        for f in rag_findings:
            if f not in state["findings"]:
                state["findings"].append(f)
        for e in rag_events:
            if e not in state["incident_events"]:
                state["incident_events"].append(e)
        
        # Preserve interrupt/pause fields if RAG requested human-in-the-loop pause
        if rag_res.get("status") == "awaiting_human":
            state["status"] = "awaiting_human"
            state["awaiting_human"] = True
            state["waiting_at"] = rag_res.get("waiting_at")
            state["interrupt_type"] = rag_res.get("interrupt_type")
            state["interrupt_question"] = rag_res.get("interrupt_question")

    # 3. METRICS
    if handle_result(metrics_res, "metrics", "correlation_agent"):
        evidence["metrics"] = {
            "metrics_data": metrics_res.get("metrics_data"),
            "metrics_summary": metrics_res.get("metrics_summary"),
            "similar_incidents": metrics_res.get("similar_incidents"),
            "root_cause": metrics_res.get("root_cause")
        }
        # Merge back to primary state for backward compatibility
        state["metrics_data"] = metrics_res.get("metrics_data")
        state["metrics_summary"] = metrics_res.get("metrics_summary")
        state["similar_incidents"] = metrics_res.get("similar_incidents")
        state["root_cause"] = metrics_res.get("root_cause")
        state["correlation_finding"] = metrics_res.get("correlation_finding")
        state["correlation"] = metrics_res.get("correlation")
        
        # Merge metrics findings and events
        metrics_findings = [f for f in metrics_res.get("findings", []) if f.get("agent") == "correlation_agent"]
        metrics_events = [e for e in metrics_res.get("incident_events", []) if e.get("source") == "correlation_agent"]
        for f in metrics_findings:
            if f not in state["findings"]:
                state["findings"].append(f)
        for e in metrics_events:
            if e not in state["incident_events"]:
                state["incident_events"].append(e)

    # 4. TOPOLOGY
    if handle_result(topology_res, "topology"):
        evidence["topology"] = topology_res
        state["services_topology"] = topology_res

    # Store unified evidence object in AnalysisState
    state["evidence"] = evidence

    # Sort findings and incident_events to match the expected sequential agent execution order
    agent_order = {
        "log_query_agent": 1,
        "rag_agent": 2,
        "correlation_agent": 3,
        "report_agent": 4
    }
    if isinstance(state.get("findings"), list):
        state["findings"].sort(key=lambda x: agent_order.get(x.get("agent"), 99))
    if isinstance(state.get("incident_events"), list):
        state["incident_events"].sort(key=lambda x: agent_order.get(x.get("source"), 99))

    # Advance current_agent to report_agent if not interrupted
    if state.get("status") != "awaiting_human":
        state["current_agent"] = "report_agent"

    return state
