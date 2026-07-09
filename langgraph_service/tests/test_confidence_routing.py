import sys
import os
import unittest
from unittest.mock import patch, MagicMock

# Ensure Python can find local imports
sys.path.append(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from workflow.graph import get_graph
from schemas.state import AnalysisState

# Mock metrics queries data
MOCK_METRICS = {
    "series": [
        {
            "metric_name": "error_rate",
            "unit": "ratio",
            "data_points": [{"timestamp": "2026-03-29T10:00:00Z", "value": 0.40}]
        },
        {
            "metric_name": "cpu",
            "unit": "ratio",
            "data_points": [{"timestamp": "2026-03-29T10:00:00Z", "value": 20.0}]
        },
        {
            "metric_name": "memory",
            "unit": "ratio",
            "data_points": [{"timestamp": "2026-03-29T10:00:00Z", "value": 30.0}]
        },
        {
            "metric_name": "db_pool_waiting",
            "unit": "ratio",
            "data_points": [{"timestamp": "2026-03-29T10:00:00Z", "value": 1.0}]
        }
    ]
}

MOCK_TOPOLOGY = {
    "generated_at": "2026-03-29T10:05:00Z",
    "services": [
        {
            "service_id": "svc-payment-api",
            "name": "payment-api",
            "health": "degraded"
        }
    ]
}


class TestConfidenceRouting(unittest.TestCase):
    @patch("agents.supervisor.GoBackendClient")
    @patch("agents.log_query_agent.GoBackendClient")
    @patch("agents.helpers.GoBackendClient")
    @patch("agents.rag_agent.GoBackendClient")
    @patch("agents.report_agent.GoBackendClient")
    @patch("agents.correlation_agent.GoBackendClient")
    def test_high_confidence_path(
        self,
        mock_correlation_client_class,
        mock_report_client_class,
        mock_rag_client_class,
        mock_helpers_client_class,
        mock_log_client_class,
        mock_supervisor_client_class
    ):
        """
        TEST 1: High confidence path
        Input: 95% confidence from correlation logic
        Expected flow: Supervisor -> Evidence Agent -> Correlation Agent -> Report Agent -> END
        """
        # Supervisor mock
        mock_supervisor = MagicMock()
        mock_supervisor.get_health.return_value = {"status": "healthy"}
        mock_supervisor.get_services.return_value = MOCK_TOPOLOGY
        mock_supervisor.create_incident.return_value = {"incident_id": "inc-high-123"}
        mock_supervisor_client_class.return_value = mock_supervisor

        # Log mock
        mock_log = MagicMock()
        mock_log.get_logs.return_value = {
            "logs": [{"id": "log-1", "level": "ERROR", "service": "payment-api", "message": "OOM error"}]
        }
        mock_log.get_log_anomalies.return_value = {"anomalous_windows": []}
        mock_log_client_class.return_value = mock_log

        # Helpers/Metrics mock
        mock_helpers = MagicMock()
        mock_helpers.get_services.return_value = MOCK_TOPOLOGY
        mock_helpers.query_metrics_batch.return_value = MOCK_METRICS
        mock_helpers.get_incidents.return_value = {"incidents": []}
        mock_helpers_client_class.return_value = mock_helpers

        # RAG mock
        mock_rag = MagicMock()
        mock_rag.search_runbooks.return_value = [
            {"runbook_id": "RB-100", "title": "Payment Fix", "similarity_score": 0.95}
        ]
        mock_rag_client_class.return_value = mock_rag

        # Report mock
        mock_report = MagicMock()
        mock_report.submit_report.return_value = {"report_id": "rep-123"}
        mock_report_client_class.return_value = mock_report

        # Correlation write mock
        mock_correlation = MagicMock()
        mock_correlation_client_class.return_value = mock_correlation

        initial_state = {
            "analysis_id": "high-conf-test",
            "alert": {
                "name": "HighErrorRate",
                "severity": "critical",
                "affected_services": ["payment-api"],
                "fired_at": "2026-03-29T10:00:00Z"
            },
            "findings": [],
            "incident_events": [],
            "status": "running",
            "current_agent": "supervisor"
        }

        graph = get_graph()
        result = graph.invoke(initial_state)

        print("CONFIDENCE:", result.get("correlation", {}).get("confidence"))
        # Execution should successfully route to report_agent and complete
        self.assertEqual(result.get("status"), "completed")
        self.assertEqual(result.get("current_agent"), "report_agent")
        
        # Structured correlation data present in state
        self.assertIn("correlation", result)
        self.assertEqual(result["correlation"]["confidence"]["level"], "HIGH")
        self.assertGreaterEqual(result["correlation"]["confidence"]["score"], 0.75)
        self.assertIsNotNone(result.get("report"))

    @patch("agents.supervisor.GoBackendClient")
    @patch("agents.log_query_agent.GoBackendClient")
    @patch("agents.helpers.GoBackendClient")
    @patch("agents.rag_agent.GoBackendClient")
    @patch("agents.correlation_agent.GoBackendClient")
    def test_low_confidence_path(
        self,
        mock_correlation_client_class,
        mock_rag_client_class,
        mock_helpers_client_class,
        mock_log_client_class,
        mock_supervisor_client_class
    ):
        """
        TEST 2: Low confidence path
        Input: Missing/weak evidence (RAG similarity >= 0.7 to avoid RAG interrupt, but empty logs, empty topology).
        Expected flow: Supervisor -> Evidence Agent -> Correlation Agent -> Human Review (paused).
        """
        # Supervisor mock (empty services topology)
        mock_supervisor = MagicMock()
        mock_supervisor.get_health.return_value = {"status": "healthy"}
        mock_supervisor.get_services.return_value = {"services": []}
        mock_supervisor.create_incident.return_value = {"incident_id": "inc-low-123"}
        mock_supervisor_client_class.return_value = mock_supervisor

        # Log mock (no logs found)
        mock_log = MagicMock()
        mock_log.get_logs.return_value = {"logs": []}
        mock_log.get_log_anomalies.return_value = {"anomalous_windows": []}
        mock_log_client_class.return_value = mock_log

        # Helpers mock (empty services topology)
        mock_helpers = MagicMock()
        mock_helpers.get_services.return_value = {"services": []}
        mock_helpers.query_metrics_batch.return_value = MOCK_METRICS
        mock_helpers.get_incidents.return_value = {"incidents": []}
        mock_helpers_client_class.return_value = mock_helpers

        # RAG mock
        mock_rag = MagicMock()
        mock_rag.search_runbooks.return_value = [
            {"runbook_id": "RB-100", "title": "Payment Fix", "similarity_score": 0.80}
        ]
        mock_rag_client_class.return_value = mock_rag

        # Correlation write mock
        mock_correlation = MagicMock()
        mock_correlation_client_class.return_value = mock_correlation

        initial_state = {
            "analysis_id": "low-conf-test",
            "alert": {
                "name": "HighErrorRate",
                "severity": "critical",
                "affected_services": ["payment-api"],
                "fired_at": "2026-03-29T10:00:00Z"
            },
            "findings": [],
            "incident_events": [],
            "status": "running",
            "current_agent": "supervisor"
        }

        graph = get_graph()
        result = graph.invoke(initial_state)

        # Execution should pause at human_review/confidence_review
        self.assertEqual(result.get("status"), "awaiting_human")
        self.assertTrue(result.get("awaiting_human"))
        self.assertEqual(result.get("waiting_at"), "confidence_review")
        self.assertEqual(result.get("interrupt_type"), "confidence_review")
        
        # Structured correlation data present in state
        self.assertIn("correlation", result)
        self.assertIn(result["correlation"]["confidence"]["level"], ["LOW", "MEDIUM"])
        self.assertLess(result["correlation"]["confidence"]["score"], 0.75)
        # Reason should record missing evidence sources
        self.assertIn("logs", result["correlation"]["confidence"]["reason"])
        self.assertIn("topology", result["correlation"]["confidence"]["reason"])

    @patch("agents.supervisor.GoBackendClient")
    @patch("agents.log_query_agent.GoBackendClient")
    @patch("agents.helpers.GoBackendClient")
    @patch("agents.rag_agent.GoBackendClient")
    @patch("agents.correlation_agent.GoBackendClient")
    def test_degraded_mode_path(
        self,
        mock_correlation_client_class,
        mock_rag_client_class,
        mock_helpers_client_class,
        mock_log_client_class,
        mock_supervisor_client_class
    ):
        """
        TEST 3: Degraded mode
        Input: One evidence source failed (logs).
        Expected flow: Correctly reduced confidence, missing source recorded, routes to human review.
        """
        # Supervisor mock
        mock_supervisor = MagicMock()
        mock_supervisor.get_health.return_value = {"status": "healthy"}
        mock_supervisor.get_services.return_value = MOCK_TOPOLOGY
        mock_supervisor.create_incident.return_value = {"incident_id": "inc-degraded-123"}
        mock_supervisor_client_class.return_value = mock_supervisor

        # Log mock: raises Exception
        mock_log = MagicMock()
        mock_log.get_logs.side_effect = Exception("Connection Refused")
        mock_log_client_class.return_value = mock_log

        # Helpers mock
        mock_helpers = MagicMock()
        mock_helpers.get_services.return_value = MOCK_TOPOLOGY
        mock_helpers.query_metrics_batch.return_value = MOCK_METRICS
        mock_helpers.get_incidents.return_value = {"incidents": []}
        mock_helpers_client_class.return_value = mock_helpers

        # RAG mock
        mock_rag = MagicMock()
        mock_rag.search_runbooks.return_value = [
            {"runbook_id": "RB-100", "title": "Payment Fix", "similarity_score": 0.95}
        ]
        mock_rag_client_class.return_value = mock_rag

        # Correlation write mock
        mock_correlation = MagicMock()
        mock_correlation_client_class.return_value = mock_correlation

        initial_state = {
            "analysis_id": "degraded-conf-test",
            "alert": {
                "name": "HighErrorRate",
                "severity": "critical",
                "affected_services": ["payment-api"],
                "fired_at": "2026-03-29T10:00:00Z"
            },
            "findings": [],
            "incident_events": [],
            "status": "running",
            "current_agent": "supervisor"
        }

        graph = get_graph()
        result = graph.invoke(initial_state)

        # The log collector failed. Score = 0.0 (logs) + 0.3 (metrics) + 0.2 (RAG) + 0.2 (topology) = 0.70.
        # 0.70 is below 0.75 threshold, so it should route to human_review/confidence_review
        self.assertEqual(result.get("status"), "awaiting_human")
        self.assertTrue(result.get("awaiting_human"))
        self.assertEqual(result.get("waiting_at"), "confidence_review")
        self.assertEqual(result["correlation"]["confidence"]["level"], "MEDIUM")
        self.assertEqual(result["correlation"]["confidence"]["score"], 0.70)
        self.assertIn("logs", result["correlation"]["confidence"]["reason"])


if __name__ == "__main__":
    unittest.main()
