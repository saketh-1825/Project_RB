import json
import os
import sys
import unittest
from datetime import datetime, timezone
from unittest.mock import MagicMock, patch

from fastapi.testclient import TestClient

sys.path.append(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from agents.correlation_agent import (
    analyze_evidence_quality,
    calculate_risk,
    correlation_agent_node,
)
from agents.evidence_agent import evidence_agent_node
from agents.report_agent import (
    build_executive_summary,
    build_incident_report,
    build_root_cause,
    build_suggested_fixes,
    build_timeline,
    extract_runbook_fixes,
    report_agent_node,
)
from internal.correlation.engine import (
    find_historical_matches,
    find_spike_time,
    infer_root_cause,
)
from internal.errors import GoBackendError
from internal.redis_client import _get_redis
from main import app
from schemas.state import AnalysisState
from workflow.graph import resume_analysis, run_analysis


def make_series(name: str, values: list) -> dict:
    """Helper to mock metrics series."""
    data_points = [
        {"timestamp": f"2026-03-29T10:{i:02d}:00Z", "value": val}
        for i, val in enumerate(values)
    ]
    return {"metric_name": name, "unit": "ratio", "data_points": data_points}


def _setup_standard_mocks(mock_classes):
    """Helper to setup standard SRE backend mocks for tests."""
    for cls in mock_classes:
        inst = cls.return_value
        inst.get_health.return_value = {"status": "ok", "components": {}}
        inst.get_services.return_value = {"services": []}
        inst.create_incident.return_value = {"incident_id": "inc-001", "status": "open"}
        inst.get_logs.return_value = {
            "logs": [
                {
                    "message": "error DB connect",
                    "timestamp": "2026-03-29T10:04:55Z",
                    "level": "ERROR",
                    "service": "payment-api",
                    "trace_id": "trace-1",
                }
            ]
        }
        inst.search_runbooks.return_value = [
            {
                "runbook_id": "RB-100",
                "title": "Troubleshoot",
                "content": "Check DB",
                "similarity_score": 0.95,
            }
        ]
        inst.post_finding.return_value = {"finding_id": "f-1"}
        inst.submit_report.return_value = {"report_id": "r-1"}
        inst._request.return_value = MagicMock(
            json=lambda: {"incidents": [], "pagination": {}}
        )
        inst.query_metrics_batch.return_value = {"series": []}
        inst.get_incidents.return_value = {"incidents": []}


class TestCorrelationLogic(unittest.TestCase):
    """Tests for correlation agent logic and root cause inference."""

    def test_find_spike_time_error_rate(self):
        """Test finding spike time from error rate series."""
        series = make_series("error_rate", [0.01, 0.02, 0.08, 0.20])
        assert find_spike_time(series).minute == 2
        assert find_spike_time(make_series("error_rate", [0.01, 0.02, 0.01])) is None

    def test_infer_root_cause_db_exhaustion(self):
        """Test DB exhaustion root cause inference."""
        metrics = {
            "db_pool_waiting": make_series("db_pool_waiting", [0.0, 3.0, 5.0, 5.0]),
            "error_rate": make_series("error_rate", [0.01, 0.01, 0.25, 0.60]),
            "cpu": make_series("cpu", [15.0, 20.0, 25.0, 30.0]),
            "memory": make_series("memory", [40.0, 40.0, 40.0, 40.0]),
        }
        res = infer_root_cause(metrics, ["payment-api"])
        assert res["type"] == "DB_EXHAUSTION"
        assert res["confidence"] == 0.92

    def test_infer_root_cause_cpu_pressure(self):
        """Test CPU pressure root cause inference."""
        metrics = {
            "db_pool_waiting": make_series("db_pool_waiting", [0.0, 0.0, 0.0, 0.0]),
            "error_rate": make_series("error_rate", [0.01, 0.01, 0.15, 0.50]),
            "cpu": make_series("cpu", [50.0, 70.0, 95.0, 80.0]),
            "memory": make_series("memory", [40.0, 40.0, 40.0, 40.0]),
        }
        assert infer_root_cause(metrics)["type"] == "CPU_PRESSURE"

    def test_infer_root_cause_memory_pressure(self):
        """Test memory pressure root cause inference."""
        metrics = {
            "db_pool_waiting": make_series("db_pool_waiting", [0.0, 0.0, 0.0, 0.0]),
            "error_rate": make_series("error_rate", [0.01, 0.01, 0.20, 0.50]),
            "cpu": make_series("cpu", [10.0, 10.0, 10.0, 10.0]),
            "memory": make_series("memory", [0.85, 0.95, 0.88, 0.85]),
        }
        assert infer_root_cause(metrics)["type"] == "MEMORY_PRESSURE"

    def test_infer_root_cause_unknown(self):
        """Test unknown root cause when no spikes match."""
        metrics = {
            "db_pool_waiting": make_series("db_pool_waiting", [0.0, 0.0, 0.0, 0.0]),
            "error_rate": make_series("error_rate", [0.01, 0.02, 0.01, 0.02]),
            "cpu": make_series("cpu", [10.0, 10.0, 10.0, 10.0]),
            "memory": make_series("memory", [40.0, 40.0, 40.0, 40.0]),
        }
        assert infer_root_cause(metrics)["type"] == "UNKNOWN"

    def test_infer_root_cause_db_spike_after_error_rate(self):
        """Test unknown root cause if db spike occurs after error spike."""
        metrics = {
            "db_pool_waiting": make_series("db_pool_waiting", [0.0, 0.0, 0.0, 5.0]),
            "error_rate": make_series("error_rate", [0.01, 0.25, 0.50, 0.60]),
            "cpu": make_series("cpu", [15.0, 20.0, 25.0, 30.0]),
            "memory": make_series("memory", [40.0, 40.0, 40.0, 40.0]),
        }
        assert infer_root_cause(metrics)["type"] == "UNKNOWN"

    def test_historical_incident_matching(self):
        """Test finding similar historical incidents based on affected services."""
        past = [
            {
                "incident_id": "inc-1",
                "affected_services": ["payment-api", "order-service"],
                "similarity_score": 1.0,
            },
            {
                "incident_id": "inc-2",
                "affected_services": ["auth-service"],
                "similarity_score": 0.5,
            },
            {
                "incident_id": "inc-3",
                "affected_services": ["api-gateway", "payment-api", "order-service"],
                "similarity_score": 1.0,
            },
        ]
        matches = find_historical_matches(past, ["payment-api", "order-service"])
        assert len(matches) == 2
        assert matches[0]["incident_id"] == "inc-1"

    def _get_correlation_state(self) -> AnalysisState:
        """Helper to get base state for correlation agent tests."""
        return {
            "analysis_id": "test-analysis-001",
            "incident_id": "inc-current-001",
            "alert": {
                "name": "HighErrorRate",
                "severity": "critical",
                "fired_at": "2026-03-29T10:00:00Z",
                "affected_services": ["payment-api"],
            },
            "findings": [],
            "incident_events": [],
            "current_agent": "correlation_agent",
        }

    @patch("agents.correlation_agent.GoBackendClient")
    def test_correlation_agent_success(self, mock_client_class):
        """Test correlation agent node standard successful execution."""
        mock_client = mock_client_class.return_value
        mock_client.query_metrics_batch.return_value = {
            "series": [
                make_series("db_pool_waiting", [0.0, 2.0, 5.0]),
                make_series("error_rate", [0.01, 0.01, 0.40]),
                make_series("cpu", [15.0] * 3),
                make_series("memory", [30.0] * 3),
            ]
        }
        mock_client.get_incidents.return_value = {
            "incidents": [
                {"incident_id": "inc-past-999", "affected_services": ["payment-api"]}
            ]
        }

        new_state = correlation_agent_node(self._get_correlation_state())

        assert new_state["current_agent"] == "report_agent"
        assert new_state["root_cause"]["type"] == "DB_EXHAUSTION"
        assert mock_client.post_finding.called

    @patch("agents.correlation_agent.GoBackendClient")
    def test_correlation_agent_degraded_mode(self, mock_client_class):
        """Test correlation agent handles backend query failures gracefully."""
        mock_client = mock_client_class.return_value
        mock_client.query_metrics_batch.side_effect = Exception("Service unavailable")
        mock_client.get_incidents.return_value = {"incidents": []}

        new_state = correlation_agent_node(self._get_correlation_state())

        assert new_state["current_agent"] == "report_agent"
        assert new_state["metrics_data"] == {}
        assert new_state["correlation_finding"]["reason"] == "METRIC_QUERY_FAILED"


class TestEvidenceCollection(unittest.TestCase):
    """Tests for evidence agent collection logic."""

    def _get_evidence_state(self) -> AnalysisState:
        """Helper to get base state for evidence agent tests."""
        return {
            "analysis_id": "test-analysis-123",
            "alert": {
                "name": "HighErrorRate",
                "severity": "critical",
                "affected_services": ["payment-api"],
                "fired_at": "2026-03-29T10:00:00Z",
            },
            "findings": [],
            "incident_events": [],
            "status": "running",
            "current_agent": "evidence_agent",
        }

    @patch("agents.log_query_agent.GoBackendClient")
    @patch("agents.correlation_agent.GoBackendClient")
    @patch("agents.rag_agent.GoBackendClient")
    @patch("agents.helpers.GoBackendClient")
    def test_evidence_agent_success(self, mock_helpers, mock_rag, mock_corr, mock_log):
        """Test evidence agent successfully collects all evidence sources."""
        _setup_standard_mocks([mock_helpers, mock_rag, mock_corr, mock_log])
        mock_helpers.return_value.get_services.return_value = {
            "services": [{"name": "payment-api"}]
        }
        mock_log.return_value.get_log_anomalies.return_value = {"anomalous_windows": []}
        mock_corr.return_value.query_metrics_batch.return_value = {
            "series": [make_series("error_rate", [0.01, 0.40])]
        }

        result = evidence_agent_node(self._get_evidence_state())

        ev = result["evidence"]
        self.assertEqual(ev["metadata"]["collection_status"]["logs"], "success")
        self.assertEqual(ev["metadata"]["collection_status"]["metrics"], "success")
        self.assertEqual(result["current_agent"], "report_agent")

    @patch("agents.log_query_agent.GoBackendClient")
    @patch("agents.correlation_agent.GoBackendClient")
    @patch("agents.rag_agent.GoBackendClient")
    @patch("agents.helpers.GoBackendClient")
    def test_evidence_agent_fault_tolerance(
        self, mock_helpers, mock_rag, mock_corr, mock_log
    ):
        """Test evidence agent gracefully handles failures in individual collectors."""
        _setup_standard_mocks([mock_helpers, mock_rag, mock_corr, mock_log])
        mock_log.return_value.get_log_anomalies.return_value = {"anomalous_windows": []}
        mock_rag.return_value.search_runbooks.side_effect = GoBackendError(
            500, "Vector DB index failed", None
        )

        result = evidence_agent_node(self._get_evidence_state())
        ev = result["evidence"]

        self.assertEqual(ev["metadata"]["collection_status"]["logs"], "success")
        self.assertEqual(ev["metadata"]["collection_status"]["rag"], "failed")
        self.assertEqual(result["current_agent"], "report_agent")


class TestEvidenceQuality(unittest.TestCase):
    """Tests for evidence quality and risk scoring."""

    def setUp(self):
        self.base_ev = {
            "metadata": {
                "collection_status": {
                    "logs": "success",
                    "metrics": "success",
                    "rag": "success",
                    "topology": "success",
                }
            },
            "logs": {
                "findings": [
                    {
                        "type": "log_anomaly",
                        "degraded": False,
                        "evidence": {"log_ids": ["123"]},
                    }
                ]
            },
            "metrics": {
                "metrics_query_failed": False,
                "metrics_response": {
                    "series": [make_series("error_rate", [0.0, 0.0, 0.8, 0.8])]
                },
            },
            "rag": {"findings": [{"type": "runbook", "similarity_score": 0.9}]},
            "topology": {"services": [{"name": "payment-api"}]},
        }
        self.base_state = {}

    def test_complete_evidence(self):
        """Test evidence quality returns 1.0 when all sources are present and aligned."""
        res = analyze_evidence_quality(self.base_ev, self.base_state)
        self.assertAlmostEqual(res["quality_score"], 1.0)
        self.assertIn("logs", res["available_sources"])

    def test_missing_metrics(self):
        """Test evidence quality is reduced when metrics are missing."""
        self.base_ev["metadata"]["collection_status"]["metrics"] = "failed"
        self.base_ev["metrics"] = {"metrics_query_failed": True}
        res = analyze_evidence_quality(self.base_ev, self.base_state)
        self.assertAlmostEqual(res["quality_score"], 0.75)
        self.assertNotIn("metrics", res["available_sources"])

    def test_conflicting_evidence(self):
        """Test evidence quality is penalized on conflicting signals."""
        self.base_ev["metrics"]["metrics_response"]["series"][0]["data_points"] = [
            {"value": 0.0},
            {"value": 0.0},
        ]
        res = analyze_evidence_quality(self.base_ev, self.base_state)
        self.assertEqual(len(res["conflicts"]), 1)
        self.assertAlmostEqual(res["quality_score"], 0.8)

    def test_risk_scoring(self):
        """Test risk level calculation based on evidence and alert severity."""
        r_crit = calculate_risk(
            self.base_ev,
            {"type": "DB_TIMEOUT", "confidence": 0.8},
            ["payment-api"],
            {"severity": "medium"},
        )
        self.assertEqual(r_crit["level"], "CRITICAL")
        weak_ev = {"metadata": {"collection_status": {"metrics": "success"}}}
        r_low = calculate_risk(
            weak_ev,
            {"type": "UNKNOWN", "confidence": 0.2},
            ["bg-worker"],
            {"severity": "low"},
        )
        self.assertEqual(r_low["level"], "LOW")


class TestReportGeneration(unittest.TestCase):
    """Tests for report generation logic."""

    def setUp(self):
        self.base_state: AnalysisState = {
            "analysis_id": "test-1",
            "incident_id": "inc-1",
            "incident_title": "DB Issue",
            "alert": {
                "name": "HighErrorRate",
                "severity": "critical",
                "fired_at": "2026-03-29T10:01:30Z",
                "affected_services": ["payment-api"],
            },
            "findings": [
                {
                    "agent": "log_query_agent",
                    "type": "log_anomaly",
                    "timestamp": "2026-03-29T10:02:15Z",
                },
                {
                    "agent": "rag_agent",
                    "type": "runbook",
                    "runbook_id": "rb-1",
                    "similarity_score": 0.94,
                    "timestamp": "2026-03-29T10:02:30Z",
                    "title": "Runbook",
                    "summary": "Fix",
                },
                {
                    "agent": "rag_agent",
                    "type": "runbook",
                    "runbook_id": "rb-4",
                    "similarity_score": 0.81,
                    "timestamp": "2026-03-29T10:02:40Z",
                    "title": "Runbook 2",
                    "summary": "Fix 2",
                },
                {
                    "agent": "correlation_agent",
                    "type": "historical_correlation",
                    "created_at": "2026-03-29T10:03:00Z",
                },
            ],
            "metrics_data": {
                "db_pool_waiting": make_series("db", [0, 3, 5, 5]),
                "error_rate": make_series("err", [0, 0, 0.25, 0.6]),
            },
            "root_cause": {
                "type": "DB_EXHAUSTION",
                "description": "DB saturation",
                "confidence": 0.92,
                "affected_services": ["payment-api"],
            },
            "current_agent": "correlation_agent",
            "incident_events": [],
        }

    def test_timeline_ordering(self):
        """Test timeline events are strictly chronological."""
        tl = build_timeline(self.base_state)
        times = [
            datetime.fromisoformat(item["time"].replace("Z", "+00:00")) for item in tl
        ]
        for i in range(len(times) - 1):
            assert times[i] <= times[i + 1]

    def test_metric_spike_before_alert(self):
        """Test timeline shows metric spikes before alert if they occurred first."""
        tl = build_timeline(self.base_state)
        db_idx = next(
            i for i, item in enumerate(tl) if "started increasing" in item["event"]
        )
        alert_idx = next(
            i for i, item in enumerate(tl) if "alert fired" in item["event"]
        )
        assert db_idx < alert_idx

    def test_root_cause_population(self):
        """Test root cause is properly formatted for the report."""
        rc = build_root_cause(self.base_state)
        assert rc["confidence"] == 0.92
        assert len(rc["supporting_findings"]) == 4

    def test_runbook_extraction(self):
        """Test runbook fixes are extracted and ordered by similarity."""
        fixes = extract_runbook_fixes(self.base_state)
        assert fixes[0]["runbook_id"] == "rb-1"
        assert fixes[0]["priority_rank"] == 1

    def test_no_runbook_fallback(self):
        """Test fallback suggested fixes when no runbook matches."""
        self.base_state["findings"] = []
        suggested = build_suggested_fixes(self.base_state)
        assert suggested[0]["action"] == "Investigate root cause manually"

    def test_executive_summary_generation(self):
        """Test executive summary includes key components."""
        summary = build_executive_summary(
            self.base_state, build_suggested_fixes(self.base_state)
        )
        assert "Payment-api experienced issues" in summary

    def test_incident_report_schema(self):
        """Test overall incident report schema completeness."""
        rep = build_incident_report(self.base_state, [], {}, [], "")
        assert rep["incident_id"] == "inc-1"
        assert "evidence_summary" in rep

    @patch("agents.report_agent.GoBackendClient")
    def test_report_agent_node_success(self, mock_client_class):
        """Test report agent node successfully generates and submits report."""
        new_state = report_agent_node(self.base_state)
        assert new_state["report_status"] == "SUBMITTED"
        assert new_state["status"] == "completed"

    @patch("agents.report_agent.GoBackendClient")
    def test_report_agent_node_failure_graceful(self, mock_client_class):
        """Test report agent handles backend submission failures gracefully."""
        mock_client_class.return_value.submit_report.side_effect = Exception("Go error")
        new_state = report_agent_node(self.base_state)
        assert new_state["report_status"] == "FAILED_TO_SUBMIT"
        assert new_state["status"] == "completed"


class TestGraphEvents(unittest.TestCase):
    """Tests for Redis graph events and streaming."""

    def setUp(self):
        self.r = _get_redis()
        self.r.flushdb()
        self.client = TestClient(app)

    def tearDown(self):
        self.r.flushdb()

    def _get_events(self, analysis_id):
        """Helper to get events for an analysis."""
        return [
            json.loads(ev)
            for ev in reversed(self.r.lrange(f"analysis:{analysis_id}:events", 0, -1))
        ]

    @patch("agents.supervisor.GoBackendClient")
    @patch("agents.log_query_agent.GoBackendClient")
    @patch("agents.helpers.GoBackendClient")
    @patch("agents.rag_agent.GoBackendClient")
    @patch("agents.report_agent.GoBackendClient")
    @patch("agents.correlation_agent.GoBackendClient")
    @patch("agents.correlation_agent.calculate_confidence")
    def test_running_and_completed_events(self, mock_calc, *mock_classes):
        """Test that a successful run emits properly formatted running/completed events."""
        _setup_standard_mocks(mock_classes)
        mock_calc.return_value = {"score": 0.85, "level": "HIGH", "reason": "OK"}

        state = {
            "analysis_id": "test-1",
            "status": "running",
            "alert": {
                "alert_id": "a-1",
                "name": "DB CPU",
                "affected_services": ["payment-api"],
                "fired_at": "2026",
            },
        }
        res = run_analysis(state)

        self.assertEqual(res.get("status"), "completed")
        events = self._get_events("test-1")
        self.assertEqual(events[0]["event_type"], "analysis.started")

        completed = [e for e in events if e.get("event_type") == "analysis.completed"]
        self.assertEqual(len(completed), 1)

    @patch("agents.supervisor.GoBackendClient")
    def test_failed_node_events(self, mock_supervisor_client):
        """Test that a crashed node correctly emits a failed event."""
        _setup_standard_mocks([mock_supervisor_client])
        mock_node = MagicMock(side_effect=RuntimeError("DB timeout"))

        state = {
            "analysis_id": "test-fail",
            "status": "running",
            "alert": {
                "alert_id": "a",
                "name": "N",
                "affected_services": ["payment-api"],
                "fired_at": "2026",
            },
        }

        with (
            patch.dict("workflow.graph.NODE_FUNCS", {"evidence_agent_node": mock_node}),
            self.assertRaises(RuntimeError),
        ):
            run_analysis(state)

        events = self._get_events("test-fail")
        failed = [
            e
            for e in events
            if e["node"] == "evidence_agent" and e["status"] == "failed"
        ]
        self.assertEqual(len(failed), 1)

    @patch("agents.supervisor.GoBackendClient")
    @patch.dict(
        "workflow.graph.NODE_FUNCS",
        {
            "evidence_agent_node": MagicMock(
                return_value={"findings": [], "status": "completed"}
            )
        },
    )
    def test_concurrent_analyses_isolation(self, mock_sup):
        """Test event isolation for concurrent analyses."""
        _setup_standard_mocks([mock_sup])

        run_analysis(
            {
                "analysis_id": "c-A",
                "status": "running",
                "alert": {
                    "alert_id": "a",
                    "name": "A",
                    "affected_services": ["s"],
                    "fired_at": "2026",
                },
            }
        )
        run_analysis(
            {
                "analysis_id": "c-B",
                "status": "running",
                "alert": {
                    "alert_id": "b",
                    "name": "B",
                    "affected_services": ["s"],
                    "fired_at": "2026",
                },
            }
        )

        events_a = self._get_events("c-A")
        events_b = self._get_events("c-B")
        self.assertTrue(all(e["analysis_id"] == "c-A" for e in events_a))
        self.assertTrue(all(e["analysis_id"] == "c-B" for e in events_b))

    @patch("agents.supervisor.GoBackendClient")
    @patch.dict(
        "workflow.graph.NODE_FUNCS",
        {
            "evidence_agent_node": MagicMock(
                return_value={
                    "analysis_id": "ws-1",
                    "status": "completed",
                    "findings": [],
                }
            )
        },
    )
    def test_websocket_event_streaming(self, mock_sup):
        """Test WebSocket client receives live updates and historical replays."""
        _setup_standard_mocks([mock_sup])
        run_analysis(
            {
                "analysis_id": "ws-1",
                "status": "running",
                "alert": {
                    "alert_id": "ws",
                    "name": "WS",
                    "affected_services": ["s"],
                    "fired_at": "2026",
                },
            }
        )

        with self.client.websocket_connect("/ws/analysis/ws-1") as ws:
            first = ws.receive_json()
            self.assertEqual(first["analysis_id"], "ws-1")
            self.assertEqual(first["event_type"], "analysis.started")


class TestHumanReviewNode(unittest.TestCase):
    """Tests for human review workflow suspension and resumption."""

    @patch("agents.supervisor.GoBackendClient")
    @patch("agents.log_query_agent.GoBackendClient")
    @patch("agents.helpers.GoBackendClient")
    @patch("agents.rag_agent.GoBackendClient")
    @patch("agents.report_agent.GoBackendClient")
    @patch("agents.correlation_agent.GoBackendClient")
    @patch("agents.correlation_agent.calculate_confidence")
    def test_human_review_flow(self, mock_calc, *mock_classes):
        """Test analysis correctly suspends for human review on low confidence and resumes."""
        _setup_standard_mocks(mock_classes)
        mock_calc.return_value = {"score": 0.3, "level": "LOW", "reason": "No logs"}

        state = {"analysis_id": "hr-1", "incident_title": "Spike"}
        res = run_analysis(state)

        self.assertEqual(res.get("status"), "awaiting_human")
        self.assertEqual(res.get("waiting_at"), "confidence_review")

        mock_calc.return_value = {
            "score": 0.85,
            "level": "HIGH",
            "reason": "Human input",
        }
        resume_payload = dict(res)
        resume_payload["human_context"] = "Confirmed"

        resumed = resume_analysis(resume_payload)
        self.assertEqual(resumed.get("status"), "completed")


class TestProductionEdgeCases(unittest.TestCase):
    """Tests for edge cases and failures in production scenarios."""

    def setUp(self):
        self.r = _get_redis()
        keys = self.r.keys("analysis:prod-scenario-*")
        if keys:
            self.r.delete(*keys)

    def tearDown(self):
        self.setUp()

    @patch("agents.supervisor.GoBackendClient")
    @patch("agents.log_query_agent.GoBackendClient")
    @patch("agents.rag_agent.GoBackendClient")
    @patch("agents.helpers.GoBackendClient")
    @patch("agents.report_agent.GoBackendClient")
    @patch("agents.correlation_agent.GoBackendClient")
    @patch("agents.correlation_agent.calculate_confidence")
    def test_scenario_4_simultaneous_alerts(self, mock_calc, *mock_classes):
        """Test two simultaneous alerts are isolated and successfully processed."""
        _setup_standard_mocks(mock_classes)
        mock_calc.return_value = {"score": 0.85, "level": "HIGH", "reason": "OK"}

        ts_a = datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")
        state_a = {
            "analysis_id": "prod-scenario-simul-A",
            "status": "running",
            "triggered_at": ts_a,
            "alert": {
                "alert_id": "a",
                "name": "DB",
                "affected_services": ["shared-db"],
                "fired_at": ts_a,
            },
        }
        state_b = {
            "analysis_id": "prod-scenario-simul-B",
            "status": "running",
            "triggered_at": ts_a,
            "alert": {
                "alert_id": "b",
                "name": "Slow",
                "affected_services": ["shared-db"],
                "fired_at": ts_a,
            },
        }

        res_a = run_analysis(state_a)
        res_b = run_analysis(state_b)

        self.assertEqual(res_a.get("status"), "completed")
        self.assertEqual(res_b.get("status"), "completed")

        ckpt_a = self.r.get("analysis:prod-scenario-simul-A:checkpoint")
        ckpt_b = self.r.get("analysis:prod-scenario-simul-B:checkpoint")
        self.assertNotEqual(ckpt_a, ckpt_b)

    @patch("agents.supervisor.GoBackendClient")
    @patch("agents.log_query_agent.GoBackendClient")
    @patch("agents.rag_agent.GoBackendClient")
    @patch("agents.helpers.GoBackendClient")
    @patch("agents.correlation_agent.GoBackendClient")
    @patch("agents.correlation_agent.calculate_confidence")
    def test_scenario_5_cancellation_failure_handling(self, mock_calc, *mock_classes):
        """Test unexpected failures do not corrupt state of concurrent healthy analyses."""
        _setup_standard_mocks(mock_classes)
        mock_calc.side_effect = RuntimeError("Failure")

        state = {
            "analysis_id": "prod-scenario-fail-001",
            "status": "running",
            "alert": {
                "alert_id": "a",
                "name": "Fail",
                "affected_services": ["svc"],
                "fired_at": "2026",
            },
        }

        with self.assertRaises(RuntimeError):
            run_analysis(state)

        mock_calc.side_effect = None
        mock_calc.return_value = {"score": 0.9, "level": "HIGH", "reason": "OK"}
        state_healthy = {
            "analysis_id": "prod-scenario-healthy-001",
            "status": "running",
            "alert": {
                "alert_id": "b",
                "name": "OK",
                "affected_services": ["svc"],
                "fired_at": "2026",
            },
        }

        res_healthy = run_analysis(state_healthy)
        self.assertEqual(res_healthy.get("status"), "completed")
