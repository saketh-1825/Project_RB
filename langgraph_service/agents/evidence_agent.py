import asyncio
import copy
import logging
from concurrent.futures import ThreadPoolExecutor
from typing import Any

from agents.helpers import collect_metrics_data, collect_topology_data
from agents.log_query_agent import log_query_agent_node
from agents.rag_agent import rag_agent_node
from schemas.state import AnalysisState

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


async def collect_metrics(state: AnalysisState) -> dict[str, Any]:
    alert = state.get("alert") or {}
    time_window = state.get("time_window")
    return await asyncio.to_thread(collect_metrics_data, alert, time_window)


async def collect_topology(state: AnalysisState) -> dict[str, Any]:
    if state.get("services_topology") is not None:
        return state.get("services_topology") or {}
    return await asyncio.to_thread(collect_topology_data)


async def orchestrate_evidence(state: AnalysisState) -> tuple:
    # Execute collectors in parallel using asyncio.gather
    return await asyncio.gather(
        collect_logs(state),
        collect_metrics(state),
        collect_rag(state),
        collect_topology(state),
        return_exceptions=True,
    )


def evidence_agent_node(state: AnalysisState) -> AnalysisState:
    """
    Coordinates and executes evidence collection from existing agents in parallel,
    building a unified evidence object and maintaining backward compatibility.
    """
    logger.info("Executing Evidence Orchestration Layer...")
    alert = state.get("alert") or {}

    # 1. Run independent collectors concurrently
    results = run_async(orchestrate_evidence(state))

    log_res = results[0]
    metrics_res = results[1]
    rag_res = results[2]
    topology_res = results[3]

    # Initialize unified evidence object
    evidence: dict[str, Any] = {
        "logs": {},
        "metrics": {},
        "rag": {},
        "topology": {},
        "metadata": {"collection_status": {}, "errors": []},
    }

    # Initialize collections in main state if not present
    if "findings" not in state or state["findings"] is None:
        state["findings"] = []
    if "incident_events" not in state or state["incident_events"] is None:
        state["incident_events"] = []

    # Helper to check for errors
    def handle_result(res, name: str, agent_name: str | None = None):
        if isinstance(res, Exception):
            evidence["metadata"]["collection_status"][name] = "failed"
            err_msg = f"{name} collection failed: {res!s}"
            logger.error(err_msg)
            evidence["metadata"]["errors"].append(err_msg)
            return False

        if agent_name and isinstance(res, dict):
            findings = res.get("findings", [])
            for f in findings:
                if f.get("agent") == agent_name and f.get("type") == "degraded":
                    evidence["metadata"]["collection_status"][name] = "failed"
                    err_msg = (
                        f"{name} collection failed: {f.get('summary', 'Unknown error')}"
                    )
                    logger.error(err_msg)
                    evidence["metadata"]["errors"].append(err_msg)
                    return False

        evidence["metadata"]["collection_status"][name] = "success"
        return True

    # 1. LOGS
    handle_result(log_res, "logs", "log_query_agent")
    if isinstance(log_res, dict):
        # Extract findings and events belonging to log_query_agent
        log_findings = [
            f
            for f in log_res.get("findings", [])
            if f.get("agent") == "log_query_agent"
        ]
        log_events = [
            e
            for e in log_res.get("incident_events", [])
            if e.get("source") == "log_query_agent"
        ]
        evidence["logs"] = {"findings": log_findings, "incident_events": log_events}
        # Merge back to primary state for backward compatibility
        for f in log_findings:
            if f not in state["findings"]:
                state["findings"].append(f)
        for e in log_events:
            if e not in state["incident_events"]:
                state["incident_events"].append(e)

    # 2. RAG
    handle_result(rag_res, "rag", "rag_agent")
    if isinstance(rag_res, dict):
        rag_findings = [
            f for f in rag_res.get("findings", []) if f.get("agent") == "rag_agent"
        ]
        rag_events = [
            e
            for e in rag_res.get("incident_events", [])
            if e.get("source") == "rag_agent"
        ]
        evidence["rag"] = {
            "findings": rag_findings,
            "incident_events": rag_events,
            "rag_query": rag_res.get("rag_query"),
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
    if handle_result(metrics_res, "metrics"):
        metrics_response = metrics_res.get("metrics_response") or {}
        series_list = metrics_response.get("series", []) if metrics_response else []
        metrics_data: dict[str, Any] = {
            str(s.get("metric_name")): s
            for s in series_list
            if isinstance(s, dict) and s.get("metric_name")
        }
        for m_name in list(metrics_data.keys()):
            if m_name == "http_error_rate":
                metrics_data["error_rate"] = metrics_data[m_name]
            elif m_name == "process_cpu_usage":
                metrics_data["cpu"] = metrics_data[m_name]
            elif m_name == "process_memory_bytes":
                metrics_data["memory"] = metrics_data[m_name]
            elif m_name == "db_pool_waiting_connections":
                metrics_data["db_pool_waiting"] = metrics_data[m_name]

        from agents.correlation_agent import _get_metric_stats
        from internal.correlation.engine import infer_root_cause

        root_cause = infer_root_cause(metrics_data, alert.get("affected_services", []))
        error_rate_stats = _get_metric_stats(
            metrics_data.get("error_rate"), scale_to_percentage=True
        )
        metrics_summary = {
            "cpu": _get_metric_stats(metrics_data.get("cpu"), scale_to_percentage=True),
            "memory": _get_metric_stats(
                metrics_data.get("memory"), scale_to_percentage=True
            ),
            "error_rate": {"max": error_rate_stats["max"]},
        }

        evidence["metrics"] = {
            "metrics_query_failed": metrics_res.get("metrics_query_failed"),
            "metrics_response": metrics_response,
            "similar_past_incidents": metrics_res.get("similar_past_incidents"),
            "metrics_data": metrics_data,
            "metrics_summary": metrics_summary,
            "similar_incidents": metrics_res.get("similar_past_incidents"),
            "root_cause": root_cause,
        }
        # Merge back to primary state for backward compatibility
        state["metrics_data"] = metrics_data
        state["metrics_summary"] = metrics_summary
        state["similar_incidents"] = metrics_res.get("similar_past_incidents")
        state["root_cause"] = root_cause

        # Set time_window if present
        if metrics_res.get("time_window"):
            state["time_window"] = metrics_res.get("time_window")

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
        "report_agent": 4,
    }
    if isinstance(state.get("findings"), list):
        state["findings"].sort(key=lambda x: agent_order.get(str(x.get("agent")), 99))
    if isinstance(state.get("incident_events"), list):
        state["incident_events"].sort(
            key=lambda x: agent_order.get(str(x.get("source")), 99)
        )

    # Advance current_agent to report_agent if not interrupted
    if state.get("status") != "awaiting_human":
        state["current_agent"] = "report_agent"

    return state
