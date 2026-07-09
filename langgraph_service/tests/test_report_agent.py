import sys
import os
from datetime import datetime, timezone, timedelta
from unittest.mock import MagicMock, patch

import pytest

sys.path.append(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from agents.report_agent import (
    build_timeline,
    build_root_cause,
    extract_runbook_fixes,
    build_suggested_fixes,
    build_executive_summary,
    build_incident_report,
    report_agent_node
)
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


@pytest.fixture
def base_state() -> AnalysisState:
    metrics = {
        "db_pool_waiting": make_series("db_pool_waiting", [0.0, 3.0, 5.0, 5.0]),  # Spikes at 10:01:00
        "error_rate": make_series("error_rate", [0.01, 0.01, 0.25, 0.60]),     # Spikes at 10:02:00
        "cpu": make_series("cpu", [15.0, 20.0, 25.0, 30.0]),
        "memory": make_series("memory", [40.0, 40.0, 40.0, 40.0])
    }
    return {
        "analysis_id": "test-analysis-001",
        "incident_id": "inc-current-001",
        "incident_title": "Database pool exhaustion",
        "alert": {
            "name": "HighErrorRate",
            "severity": "critical",
            "fired_at": "2026-03-29T10:01:30Z",  # Alert fired between spikes
            "affected_services": ["payment-api"]
        },
        "findings": [
            {
                "agent": "log_query_agent",
                "type": "log_anomaly",
                "severity": "high",
                "title": "Error spike detected",
                "summary": "Large number of ERROR logs found",
                "timestamp": "2026-03-29T10:02:15Z"
            },
            {
                "agent": "rag_agent",
                "type": "runbook",
                "runbook_id": "rb-001",
                "title": "Increase PostgreSQL connection pool",
                "summary": "Increase max_open_connections and investigate leaks",
                "similarity_score": 0.94,
                "timestamp": "2026-03-29T10:02:30Z"
            },
            {
                "agent": "rag_agent",
                "type": "runbook",
                "runbook_id": "rb-004",
                "title": "Terminate long running transactions",
                "summary": "Kill active connections that are slow",
                "similarity_score": 0.81,
                "timestamp": "2026-03-29T10:02:40Z"
            },
            {
                "agent": "correlation_agent",
                "type": "historical_correlation",
                "title": "Root Cause Correlation Analysis: DB_EXHAUSTION",
                "summary": "Database connection pool saturation caused request failures",
                "created_at": "2026-03-29T10:03:00Z"
            }
        ],
        "metrics_data": metrics,
        "metrics_summary": {
            "error_rate": {"max": 18},
            "cpu": {"max": 72},
            "memory": {"max": 81}
        },
        "root_cause": {
            "type": "DB_EXHAUSTION",
            "description": "Database connection pool saturation caused request failures",
            "confidence": 0.92,
            "affected_services": ["payment-api"]
        },
        "incident_events": [],
        "current_agent": "correlation_agent"
    }


def test_timeline_ordering(base_state):
    timeline = build_timeline(base_state)
    assert len(timeline) >= 6

    # Parse and check chronological ordering
    times = []
    for item in timeline:
        assert "time" in item
        assert "event" in item
        # Parse Z ISO timestamp
        parsed_dt = datetime.fromisoformat(item["time"].replace("Z", "+00:00"))
        times.append(parsed_dt)

    # Check ascending order
    for i in range(len(times) - 1):
        assert times[i] <= times[i+1]


def test_metric_spike_before_alert(base_state):
    timeline = build_timeline(base_state)

    # Look for the metric spike and alert events
    db_spike_index = -1
    alert_index = -1
    for idx, item in enumerate(timeline):
        if "db_pool_waiting started increasing" in item["event"]:
            db_spike_index = idx
        elif "HighErrorRate alert fired" in item["event"]:
            alert_index = idx

    assert db_spike_index != -1
    assert alert_index != -1
    # Check that db_pool_waiting spike happened before the alert fired
    assert db_spike_index < alert_index


def test_root_cause_population(base_state):
    rc = build_root_cause(base_state)
    assert rc["description"] == "Database connection pool saturation caused request failures"
    assert rc["affected_services"] == ["payment-api"]
    assert rc["confidence"] == 0.92
    assert len(rc["supporting_findings"]) == 4

    # Supporting findings should map correct fields
    first_sf = rc["supporting_findings"][0]
    assert "agent" in first_sf
    assert "type" in first_sf
    assert "title" in first_sf
    assert "summary" in first_sf


def test_runbook_extraction(base_state):
    fixes = extract_runbook_fixes(base_state)
    assert len(fixes) == 2

    # Should be sorted by similarity_score descending
    assert fixes[0]["runbook_id"] == "rb-001"
    assert fixes[0]["similarity_score"] == 0.94
    assert fixes[0]["priority"] == "HIGH"
    assert fixes[0]["priority_rank"] == 1

    assert fixes[1]["runbook_id"] == "rb-004"
    assert fixes[1]["similarity_score"] == 0.81
    assert fixes[1]["priority"] == "MEDIUM"
    assert fixes[1]["priority_rank"] == 2


def test_suggested_fixes_ordering(base_state):
    suggested = build_suggested_fixes(base_state)
    assert len(suggested) == 2
    assert suggested[0]["priority_rank"] == 1
    assert suggested[1]["priority_rank"] == 2


def test_no_runbook_fallback(base_state):
    # Clear runbooks from findings
    base_state["findings"] = [
        f for f in base_state["findings"] if f.get("type") != "runbook"
    ]
    suggested = build_suggested_fixes(base_state)
    assert len(suggested) == 1
    assert suggested[0]["priority_rank"] == 1
    assert suggested[0]["title"] == "Investigate root cause"
    assert suggested[0]["action"] == "Investigate root cause manually"


def test_executive_summary_generation(base_state):
    suggested = build_suggested_fixes(base_state)
    summary = build_executive_summary(base_state, suggested)

    assert "Payment-api experienced issues likely caused by database connection pool saturation" in summary
    assert "Recommended action: Increase PostgreSQL connection pool." in summary


def test_incident_report_schema(base_state):
    timeline = build_timeline(base_state)
    rc = build_root_cause(base_state)
    suggested = build_suggested_fixes(base_state)
    summary = build_executive_summary(base_state, suggested)

    report = build_incident_report(base_state, timeline, rc, suggested, summary)

    assert report["incident_id"] == "inc-current-001"
    assert report["title"] == "Database pool exhaustion"
    assert report["executive_summary"] == summary
    assert report["timeline"] == timeline
    assert report["root_cause"] == rc
    assert report["suggested_fixes"] == suggested
    assert "evidence_summary" in report
    assert "missing_evidence" in report
    assert "risk_assessment" in report
    assert "created_at" in report
    # Created at ISO normalization validation
    assert report["created_at"].endswith("Z")


@patch("agents.report_agent.GoBackendClient")
def test_report_agent_node_success(mock_client_class, base_state):
    mock_client = MagicMock()
    mock_client_class.return_value = mock_client

    new_state = report_agent_node(base_state)

    # Verify state updates
    assert new_state["report_status"] == "SUBMITTED"
    assert new_state["status"] == "completed"
    assert new_state["current_agent"] == "report_agent"
    assert new_state["awaiting_human"] is False
    assert "report" in new_state
    assert new_state["report"]["incident_id"] == "inc-current-001"

    # Verify submit_report call
    mock_client.submit_report.assert_called_once_with(
        "inc-current-001",
        new_state["report"]
    )


@patch("agents.report_agent.GoBackendClient")
def test_report_agent_node_failure_graceful(mock_client_class, base_state):
    mock_client = MagicMock()
    mock_client.submit_report.side_effect = Exception("Go backend error")
    mock_client_class.return_value = mock_client

    new_state = report_agent_node(base_state)

    # Verify graceful degradation
    assert new_state["report_status"] == "FAILED_TO_SUBMIT"
    assert new_state["status"] == "completed"
    assert "report" in new_state
    # Check that it did not crash and returned updated state
    assert new_state["report"]["incident_id"] == "inc-current-001"
