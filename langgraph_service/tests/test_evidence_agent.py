import sys
import os
import unittest
from unittest.mock import patch, MagicMock
from datetime import datetime, timezone

# Ensure Python can find local imports
sys.path.append(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from schemas.state import AnalysisState
from agents.evidence_agent import evidence_agent_node
from internal.errors import GoBackendError

# Mock response data for testing
MOCK_LOGS = {
    "logs": [
        {
            "id": "log-1",
            "timestamp": "2026-03-29T10:04:55Z",
            "level": "ERROR",
            "service": "payment-api",
            "message": "Out of memory",
            "trace_id": "trace-123"
        }
    ]
}

def make_mock_series(name: str, values: list) -> dict:
    data_points = [{"timestamp": f"2026-03-29T10:{i:02d}:00Z", "value": val} for i, val in enumerate(values)]
    return {
        "metric_name": name,
        "unit": "ratio",
        "data_points": data_points
    }

MOCK_METRICS = {
    "series": [
        make_mock_series("error_rate", [0.01, 0.02, 0.40]),
        make_mock_series("cpu", [10.0, 15.0, 20.0]),
        make_mock_series("memory", [30.0, 30.0, 30.0]),
        make_mock_series("db_pool_waiting", [0.0, 0.0, 1.0])
    ]
}

MOCK_RUNBOOKS = [
    {
        "runbook_id": "RB-100",
        "title": "Payment Latency Troubleshooting",
        "content": "Check DB pool settings",
        "similarity_score": 0.95
    }
]

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


class TestEvidenceAgent(unittest.TestCase):
    @patch("agents.log_query_agent.GoBackendClient")
    @patch("agents.correlation_agent.GoBackendClient")
    @patch("agents.rag_agent.GoBackendClient")
    @patch("agents.helpers.GoBackendClient")
    def test_evidence_agent_success(
        self,
        mock_helpers_client_class,
        mock_rag_client_class,
        mock_corr_client_class,
        mock_log_client_class
    ):
        # Setup mocks
        mock_log_client = MagicMock()
        mock_log_client.get_logs.return_value = MOCK_LOGS
        mock_log_client.get_log_anomalies.return_value = {"anomalous_windows": []}
        mock_log_client_class.return_value = mock_log_client

        mock_corr_client = MagicMock()
        mock_corr_client.query_metrics_batch.return_value = MOCK_METRICS
        mock_corr_client.get_incidents.return_value = {"incidents": []}
        mock_corr_client_class.return_value = mock_corr_client

        mock_rag_client = MagicMock()
        mock_rag_client.search_runbooks.return_value = MOCK_RUNBOOKS
        mock_rag_client_class.return_value = mock_rag_client

        mock_helpers_client = MagicMock()
        mock_helpers_client.get_services.return_value = MOCK_TOPOLOGY
        mock_helpers_client_class.return_value = mock_helpers_client

        # Initial state setup
        state: AnalysisState = {
            "analysis_id": "test-analysis-123",
            "alert": {
                "name": "HighErrorRate",
                "severity": "critical",
                "affected_services": ["payment-api"],
                "fired_at": "2026-03-29T10:00:00Z"
            },
            "findings": [],
            "incident_events": [],
            "status": "running",
            "current_agent": "evidence_agent",
            "custom_metadata_field": "intact_value" # Verify existing state data is unchanged
        }

        # Run node
        result = evidence_agent_node(state)

        # 1. Verify evidence agent ran successfully and populated state["evidence"]
        self.assertIn("evidence", result)
        evidence = result["evidence"]

        # 2. Verify existing state data remains unchanged
        self.assertEqual(result["custom_metadata_field"], "intact_value")

        # 3. Verify logs evidence collected
        self.assertEqual(evidence["metadata"]["collection_status"]["logs"], "success")
        self.assertGreater(len(evidence["logs"]["findings"]), 0)

        # 4. Verify metrics evidence collected
        self.assertEqual(evidence["metadata"]["collection_status"]["metrics"], "success")
        self.assertIsNotNone(evidence["metrics"]["metrics_data"])

        # 5. Verify RAG evidence collected
        self.assertEqual(evidence["metadata"]["collection_status"]["rag"], "success")
        self.assertEqual(evidence["rag"]["findings"][0]["runbook_id"], "RB-100")

        # 6. Verify topology evidence collected
        self.assertEqual(evidence["metadata"]["collection_status"]["topology"], "success")
        self.assertEqual(evidence["topology"]["services"][0]["name"], "payment-api")

        # Verify merge compatibility with AnalysisState fields
        self.assertEqual(result["services_topology"]["services"][0]["name"], "payment-api")
        self.assertEqual(result["current_agent"], "report_agent")

    @patch("agents.log_query_agent.GoBackendClient")
    @patch("agents.correlation_agent.GoBackendClient")
    @patch("agents.rag_agent.GoBackendClient")
    @patch("agents.helpers.GoBackendClient")
    def test_evidence_agent_fault_tolerance(
        self,
        mock_helpers_client_class,
        mock_rag_client_class,
        mock_corr_client_class,
        mock_log_client_class
    ):
        # Setup mocks: RAG collector fails, others succeed
        mock_log_client = MagicMock()
        mock_log_client.get_logs.return_value = MOCK_LOGS
        mock_log_client.get_log_anomalies.return_value = {"anomalous_windows": []}
        mock_log_client_class.return_value = mock_log_client

        mock_corr_client = MagicMock()
        mock_corr_client.query_metrics_batch.return_value = MOCK_METRICS
        mock_corr_client.get_incidents.return_value = {"incidents": []}
        mock_corr_client_class.return_value = mock_corr_client

        # RAG fails by raising an exception
        mock_rag_client = MagicMock()
        mock_rag_client.search_runbooks.side_effect = GoBackendError(500, "Vector DB index failed", None)
        mock_rag_client_class.return_value = mock_rag_client

        mock_helpers_client = MagicMock()
        mock_helpers_client.get_services.return_value = MOCK_TOPOLOGY
        mock_helpers_client_class.return_value = mock_helpers_client

        # Initial state setup
        state: AnalysisState = {
            "analysis_id": "test-analysis-456",
            "alert": {
                "name": "HighErrorRate",
                "severity": "critical",
                "affected_services": ["payment-api"],
                "fired_at": "2026-03-29T10:00:00Z"
            },
            "findings": [],
            "incident_events": [],
            "status": "running",
            "current_agent": "evidence_agent"
        }

        # Run node
        result = evidence_agent_node(state)

        # 7. Verify one failed collector does not break overall execution
        self.assertIn("evidence", result)
        evidence = result["evidence"]

        # Check logs and metrics succeeded
        self.assertEqual(evidence["metadata"]["collection_status"]["logs"], "success")
        self.assertEqual(evidence["metadata"]["collection_status"]["metrics"], "success")
        self.assertEqual(evidence["metadata"]["collection_status"]["topology"], "success")

        # Check RAG failed
        self.assertEqual(evidence["metadata"]["collection_status"]["rag"], "failed")
        self.assertTrue(any("rag collection failed" in err.lower() for err in evidence["metadata"]["errors"]))

        # Even with failed RAG, the graph proceeds and merges the rest of the evidence
        self.assertEqual(result["current_agent"], "report_agent")


if __name__ == "__main__":
    unittest.main()
