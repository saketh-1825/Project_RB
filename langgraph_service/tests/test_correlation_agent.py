import sys
import os
from datetime import datetime, timezone
from unittest.mock import MagicMock, patch

import pytest

# Ensure python path works inside the container
sys.path.append(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from internal.correlation.engine import (
    find_spike_time,
    infer_root_cause,
    find_historical_matches,
    build_correlation_finding
)
from agents.correlation_agent import correlation_agent_node
from schemas.state import AnalysisState


def make_series(name: str, values: list) -> dict:
    """Constructs a mock metric series with timestamps spaced 1m apart starting from 10:00."""
    data_points = []
    for i, val in enumerate(values):
        ts = f"2026-03-29T10:{i:02d}:00Z"
        data_points.append({"timestamp": ts, "value": val})
    return {
        "metric_name": name,
        "unit": "ratio",
        "data_points": data_points
    }


# ─── 1. Spike Time Tests ─────────────────────────────────────────────────────

def test_find_spike_time_error_rate():
    # error_rate threshold is > 0.05
    series = make_series("error_rate", [0.01, 0.02, 0.08, 0.20])
    spike = find_spike_time(series)
    assert spike is not None
    assert spike.minute == 2

    series_flat = make_series("error_rate", [0.01, 0.02, 0.01])
    assert find_spike_time(series_flat) is None


# ─── 2. Root Cause Verification Tests ────────────────────────────────────────

def test_infer_root_cause_db_exhaustion():
    # db_pool_waiting spikes at 10:01, error_rate spikes at 10:02
    metrics = {
        "db_pool_waiting": make_series("db_pool_waiting", [0.0, 3.0, 5.0, 5.0]),
        "error_rate": make_series("error_rate", [0.01, 0.01, 0.25, 0.60]),
        "cpu": make_series("cpu", [15.0, 20.0, 25.0, 30.0]),
        "memory": make_series("memory", [40.0, 40.0, 40.0, 40.0])
    }
    result = infer_root_cause(metrics, ["payment-api"])
    assert result["type"] == "DB_EXHAUSTION"
    assert result["confidence"] == 0.92
    assert "Database connection pool saturation" in result["description"]
    assert result["affected_services"] == ["payment-api"]


def test_infer_root_cause_cpu_pressure():
    # CPU is > 90 (at 10:02 CPU=95) and error rate spikes (at 10:02)
    metrics = {
        "db_pool_waiting": make_series("db_pool_waiting", [0.0, 0.0, 0.0, 0.0]),
        "error_rate": make_series("error_rate", [0.01, 0.01, 0.15, 0.50]),
        "cpu": make_series("cpu", [50.0, 70.0, 95.0, 80.0]),
        "memory": make_series("memory", [40.0, 40.0, 40.0, 40.0])
    }
    result = infer_root_cause(metrics)
    assert result["type"] == "CPU_PRESSURE"
    assert result["confidence"] == 0.80


def test_infer_root_cause_memory_pressure():
    # Memory is > 90 (at 10:01 memory=0.95 ratio) and error rate spikes (at 10:02)
    metrics = {
        "db_pool_waiting": make_series("db_pool_waiting", [0.0, 0.0, 0.0, 0.0]),
        "error_rate": make_series("error_rate", [0.01, 0.01, 0.20, 0.50]),
        "cpu": make_series("cpu", [10.0, 10.0, 10.0, 10.0]),
        "memory": make_series("memory", [0.85, 0.95, 0.88, 0.85])
    }
    result = infer_root_cause(metrics)
    assert result["type"] == "MEMORY_PRESSURE"
    assert result["confidence"] == 0.80


def test_infer_root_cause_unknown():
    # No spikes
    metrics = {
        "db_pool_waiting": make_series("db_pool_waiting", [0.0, 0.0, 0.0, 0.0]),
        "error_rate": make_series("error_rate", [0.01, 0.02, 0.01, 0.02]),
        "cpu": make_series("cpu", [10.0, 10.0, 10.0, 10.0]),
        "memory": make_series("memory", [40.0, 40.0, 40.0, 40.0])
    }
    result = infer_root_cause(metrics)
    assert result["type"] == "UNKNOWN"
    assert result["confidence"] == 0.30


def test_infer_root_cause_db_spike_after_error_rate():
    # db_pool_waiting spikes at 10:03, but error_rate spiked earlier at 10:01 -> UNKNOWN
    metrics = {
        "db_pool_waiting": make_series("db_pool_waiting", [0.0, 0.0, 0.0, 5.0]),
        "error_rate": make_series("error_rate", [0.01, 0.25, 0.50, 0.60]),
        "cpu": make_series("cpu", [15.0, 20.0, 25.0, 30.0]),
        "memory": make_series("memory", [40.0, 40.0, 40.0, 40.0])
    }
    result = infer_root_cause(metrics)
    assert result["type"] == "UNKNOWN"
    assert result["confidence"] == 0.30


# ─── 3. Historical Matching Tests ─────────────────────────────────────────────

def test_historical_incident_matching():
    past_incidents = [
        {
            "incident_id": "inc-1",
            "title": "DB failure",
            "affected_services": ["payment-api", "order-service"],
            "root_cause_summary": "DB lock wait timeout"
        },
        {
            "incident_id": "inc-2",
            "title": "Auth failure",
            "affected_services": ["auth-service"],
            "root_cause_summary": "Expired auth secret key"
        },
        {
            "incident_id": "inc-3",
            "title": "Gateway failure",
            "affected_services": ["api-gateway", "payment-api", "order-service"],
            "root_cause_summary": "Incorrect ingress controller config"
        }
    ]

    current_services = ["payment-api", "order-service"]

    matches = find_historical_matches(past_incidents, current_services)
    assert len(matches) == 2
    assert matches[0]["incident_id"] == "inc-1"
    assert matches[0]["similarity_score"] == 1.0
    assert matches[1]["incident_id"] == "inc-3"
    assert matches[1]["similarity_score"] == 1.0


# ─── 4. Agent Execution & Degradation Tests ────────────────────────────────────

@patch("agents.correlation_agent.GoBackendClient")
def test_correlation_agent_success(mock_client_class):
    mock_client = MagicMock()
    mock_client_class.return_value = mock_client

    # Mock success responses
    mock_client.query_metrics_batch.return_value = {
        "series": [
            make_series("db_pool_waiting", [0.0, 2.0, 5.0]),
            make_series("error_rate", [0.01, 0.01, 0.40]),
            make_series("cpu", [15.0, 15.0, 15.0]),
            make_series("memory", [30.0, 30.0, 30.0])
        ]
    }
    mock_client.get_incidents.return_value = {
        "incidents": [
            {
                "incident_id": "inc-past-999",
                "title": "Old DB Pool Exhaustion",
                "affected_services": ["payment-api"],
                "root_cause_summary": "Runaway queries holding pg connections"
            }
        ]
    }

    state: AnalysisState = {
        "analysis_id": "test-analysis-001",
        "incident_id": "inc-current-001",
        "alert": {
            "name": "HighErrorRate",
            "severity": "critical",
            "fired_at": "2026-03-29T10:00:00Z",
            "affected_services": ["payment-api"]
        },
        "findings": [],
        "incident_events": [],
        "current_agent": "correlation_agent"
    }

    new_state = correlation_agent_node(state)

    # Verify agent node updates
    assert new_state["current_agent"] == "report_agent"
    assert new_state["root_cause"]["type"] == "DB_EXHAUSTION"
    assert new_state["root_cause"]["confidence"] == 0.92
    assert "error_rate" in new_state["metrics_summary"]
    assert new_state["metrics_summary"]["cpu"]["max"] == 15
    assert len(new_state["similar_incidents"]) == 1

    # Verify posted finding
    mock_client.post_finding.assert_called_once()
    posted_finding = mock_client.post_finding.call_args[0][1]
    assert posted_finding["agent"] == "correlation_agent"
    assert posted_finding["confidence"] == 0.92
    assert posted_finding["severity"] == "high"


@patch("agents.correlation_agent.GoBackendClient")
def test_correlation_agent_degraded_mode(mock_client_class):
    mock_client = MagicMock()
    mock_client_class.return_value = mock_client

    # query_metrics_batch raises Exception (API down)
    mock_client.query_metrics_batch.side_effect = Exception("Service unavailable")
    mock_client.get_incidents.return_value = {"incidents": []}

    state: AnalysisState = {
        "analysis_id": "test-analysis-002",
        "incident_id": "inc-current-002",
        "alert": {
            "name": "HighErrorRate",
            "severity": "critical",
            "fired_at": "2026-03-29T10:00:00Z",
            "affected_services": ["payment-api"]
        },
        "findings": [],
        "incident_events": [],
        "current_agent": "correlation_agent"
    }

    new_state = correlation_agent_node(state)

    # Verify agent node degraded gracefully
    assert new_state["current_agent"] == "report_agent"
    assert new_state["metrics_data"] == {}
    assert new_state["metrics_summary"] == {}

    # Verify degraded finding is created as specified
    finding = new_state["correlation_finding"]
    assert finding["type"] == "historical_correlation"
    assert finding["confidence"] == 0.2
    assert finding["summary"] == "Metrics endpoint unavailable"
    assert finding["reason"] == "METRIC_QUERY_FAILED"
