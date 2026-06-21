import sys
import os
import unittest
from datetime import datetime
from unittest.mock import patch, MagicMock

# Ensure python can find local imports
sys.path.append(os.path.dirname(os.path.abspath(__file__)))

from internal.errors import (
    GoBackendError,
    BackendTimeoutError,
    BackendUnavailableError,
    BackendNotFoundError
)
from agents.helpers import build_degraded_finding
from prompts import load_prompt
from workflow.graph import get_graph


class TestErrorHandlingAndPrompts(unittest.TestCase):

    # ---------------------------------------------------
    # Exception & Helper Unit Tests
    # ---------------------------------------------------

    def test_custom_exceptions_and_original_exception(self):
        orig_exc = ValueError("Inner error details")
        
        # Test BackendNotFoundError (404 -> not_found)
        err_404 = BackendNotFoundError(404, "Not Found Error", orig_exc)
        self.assertEqual(err_404.status_code, 404)
        self.assertEqual(err_404.message, "Not Found Error")
        self.assertEqual(err_404.original_exception, orig_exc)
        self.assertEqual(err_404.error_category, "not_found")
        
        # Test BackendTimeoutError (504 -> timeout)
        err_504 = BackendTimeoutError(504, "Timeout Error", orig_exc)
        self.assertEqual(err_504.status_code, 504)
        self.assertEqual(err_504.original_exception, orig_exc)
        self.assertEqual(err_504.error_category, "timeout")
        
        # Test BackendUnavailableError (500 -> server_error)
        err_500 = BackendUnavailableError(500, "Server Error", orig_exc)
        self.assertEqual(err_500.status_code, 500)
        self.assertEqual(err_500.original_exception, orig_exc)
        self.assertEqual(err_500.error_category, "server_error")
        
        # Test BackendUnavailableError (503 -> backend_unavailable)
        err_503 = BackendUnavailableError(503, "Service Unavailable", orig_exc)
        self.assertEqual(err_503.status_code, 503)
        self.assertEqual(err_503.original_exception, orig_exc)
        self.assertEqual(err_503.error_category, "backend_unavailable")

    def test_build_degraded_finding_fields(self):
        finding = build_degraded_finding(
            agent="rag_agent",
            status_code=504,
            message="Runbook lookup failed",
            error_category="timeout"
        )
        
        self.assertEqual(finding["agent"], "rag_agent")
        self.assertEqual(finding["type"], "degraded")
        self.assertTrue(finding["degraded"])
        self.assertEqual(finding["severity"], "medium")
        self.assertEqual(finding["title"], "Backend unavailable")
        self.assertEqual(finding["summary"], "Runbook lookup failed")
        self.assertEqual(finding["confidence"], 0.2)
        self.assertEqual(finding["error_category"], "timeout")
        self.assertEqual(finding["evidence"]["backend"], "mock-go-backend")
        self.assertEqual(finding["evidence"]["status_code"], 504)
        
        # Verify timestamp format (ISO 8601 validation)
        timestamp_str = finding["timestamp"]
        self.assertIsNotNone(timestamp_str)
        # Should parse successfully
        parsed = datetime.fromisoformat(timestamp_str.replace("Z", "+00:00"))
        self.assertIsInstance(parsed, datetime)

    # ---------------------------------------------------
    # Prompt Loading Tests
    # ---------------------------------------------------

    def test_prompt_files_load_correctly(self):
        # Assert each expected prompt file loads without error
        rag_query = load_prompt("rag_query_prompt.txt")
        self.assertIn("SRE retrieval assistant", rag_query)
        self.assertIn("{title}", rag_query)

        interrupt_prompt = load_prompt("interrupt_question_prompt.txt")
        self.assertIn("SRE investigation", interrupt_prompt)

        correlation_prompt = load_prompt("correlation_prompt.txt")
        self.assertIn("Site Reliability Engineer", correlation_prompt)

        report_prompt = load_prompt("report_prompt.txt")
        self.assertIn("SRE incident reporting assistant", report_prompt)

        # Assert invalid prompt file raises FileNotFoundError
        with self.assertRaises(FileNotFoundError):
            load_prompt("non_existent_prompt_template.txt")

    # ---------------------------------------------------
    # Workflow Integration Tests (with Mocked Backend Calls)
    # ---------------------------------------------------

    @patch("internal.client.go_backend.GoBackendClient.submit_report")
    @patch("internal.client.go_backend.GoBackendClient.post_finding")
    @patch("internal.client.go_backend.GoBackendClient._request")
    @patch("internal.client.go_backend.GoBackendClient.create_incident")
    @patch("internal.client.go_backend.GoBackendClient.get_services")
    @patch("internal.client.go_backend.GoBackendClient.get_health")
    @patch("internal.client.go_backend.GoBackendClient.get_logs")
    @patch("internal.client.go_backend.GoBackendClient.search_runbooks")
    def test_normal_operation_works_fine(self, mock_search, mock_get_logs,
                                         mock_health, mock_services,
                                         mock_create_incident, mock_request,
                                         mock_post_finding, mock_submit_report):
        # Mimic normal successful backend response
        mock_health.return_value = {"status": "ok", "components": {}}
        mock_services.return_value = {"services": []}
        mock_create_incident.return_value = {"incident_id": "inc-test-001", "status": "open"}
        mock_get_logs.return_value = {
            "logs": [
                {"id": "log-1", "severity": "ERROR", "message": "Connection timeout", "trace_id": "tr-123"}
            ]
        }
        mock_search.return_value = [
            {
                "runbook_id": "rb-100",
                "title": "Payment Latency Troubleshooting",
                "content": "Step 1: Check database logs",
                "similarity_score": 0.95
            }
        ]
        mock_resp = MagicMock()
        mock_resp.json.return_value = {"incidents": [], "pagination": {}}
        mock_request.return_value = mock_resp
        mock_post_finding.return_value = {"finding_id": "f-1", "stored_at": "now"}
        mock_submit_report.return_value = {"report_id": "r-1", "stored_at": "now"}

        graph = get_graph()
        initial_state = {
            "incident_title": "Payment API Latency Spike",
            "incident_summary": "P95 latency exceeded threshold"
        }
        result = graph.invoke(initial_state)

        # Normal execution should yield regular findings and complete successfully
        self.assertEqual(result.get("status"), "completed")
        findings = result.get("findings", [])
        # 4 findings: log_query_agent + rag_agent + correlation_agent + report_agent
        self.assertEqual(len(findings), 4)
        
        self.assertEqual(findings[0]["agent"], "log_query_agent")
        self.assertEqual(findings[0]["type"], "log_anomaly")
        self.assertEqual(findings[1]["agent"], "rag_agent")
        self.assertEqual(findings[1]["type"], "runbook")
        self.assertEqual(findings[1]["runbook_id"], "rb-100")
        self.assertEqual(findings[2]["agent"], "correlation_agent")
        self.assertEqual(findings[3]["agent"], "report_agent")

    @patch("internal.client.go_backend.GoBackendClient.submit_report")
    @patch("internal.client.go_backend.GoBackendClient.post_finding")
    @patch("internal.client.go_backend.GoBackendClient._request")
    @patch("internal.client.go_backend.GoBackendClient.create_incident")
    @patch("internal.client.go_backend.GoBackendClient.get_services")
    @patch("internal.client.go_backend.GoBackendClient.get_health")
    @patch("internal.client.go_backend.GoBackendClient.get_logs")
    @patch("internal.client.go_backend.GoBackendClient.search_runbooks")
    def test_log_agent_degradation_timeout(self, mock_search, mock_get_logs,
                                            mock_health, mock_services,
                                            mock_create_incident, mock_request,
                                            mock_post_finding, mock_submit_report):
        # Supervisor mocks
        mock_health.return_value = {"status": "ok", "components": {}}
        mock_services.return_value = {"services": []}
        mock_create_incident.return_value = {"incident_id": "inc-test-002", "status": "open"}
        mock_resp = MagicMock()
        mock_resp.json.return_value = {"incidents": [], "pagination": {}}
        mock_request.return_value = mock_resp
        mock_post_finding.return_value = {"finding_id": "f-1", "stored_at": "now"}
        mock_submit_report.return_value = {"report_id": "r-1", "stored_at": "now"}

        # get_logs raises BackendTimeoutError (504)
        orig_exc = Exception("Connection timeout")
        mock_get_logs.side_effect = BackendTimeoutError(504, "Gateway Timeout", orig_exc)

        # search_runbooks is normal
        mock_search.return_value = [
            {
                "runbook_id": "rb-100",
                "title": "Payment Latency Troubleshooting",
                "content": "Step 1: Check database logs",
                "similarity_score": 0.95
            }
        ]

        graph = get_graph()
        initial_state = {
            "incident_title": "Payment API Latency Spike",
            "incident_summary": "P95 latency exceeded threshold"
        }
        result = graph.invoke(initial_state)

        # Verify workflow continued to RAG and completed
        self.assertEqual(result.get("status"), "completed")
        findings = result.get("findings", [])
        # 4 findings: degraded log + runbook + correlation + report
        self.assertEqual(len(findings), 4)

        # Finding 0: degraded from log_query_agent
        self.assertEqual(findings[0]["agent"], "log_query_agent")
        self.assertEqual(findings[0]["type"], "degraded")
        self.assertTrue(findings[0]["degraded"])
        self.assertEqual(findings[0]["error_category"], "timeout")
        self.assertEqual(findings[0]["evidence"]["status_code"], 504)
        self.assertEqual(findings[0]["evidence"]["backend"], "mock-go-backend")

        # Finding 1: normal runbook from rag_agent
        self.assertEqual(findings[1]["agent"], "rag_agent")
        self.assertEqual(findings[1]["type"], "runbook")

        # Verify events include degraded entry
        events = result.get("incident_events", [])
        self.assertGreaterEqual(len(events), 2)
        self.assertEqual(events[0]["event_type"], "degraded")
        self.assertEqual(events[0]["source"], "log_query_agent")

    @patch("internal.client.go_backend.GoBackendClient.submit_report")
    @patch("internal.client.go_backend.GoBackendClient.post_finding")
    @patch("internal.client.go_backend.GoBackendClient._request")
    @patch("internal.client.go_backend.GoBackendClient.create_incident")
    @patch("internal.client.go_backend.GoBackendClient.get_services")
    @patch("internal.client.go_backend.GoBackendClient.get_health")
    @patch("internal.client.go_backend.GoBackendClient.get_logs")
    @patch("internal.client.go_backend.GoBackendClient.search_runbooks")
    def test_rag_agent_degradation_not_found(self, mock_search, mock_get_logs,
                                              mock_health, mock_services,
                                              mock_create_incident, mock_request,
                                              mock_post_finding, mock_submit_report):
        # Supervisor mocks
        mock_health.return_value = {"status": "ok", "components": {}}
        mock_services.return_value = {"services": []}
        mock_create_incident.return_value = {"incident_id": "inc-test-003", "status": "open"}
        mock_resp = MagicMock()
        mock_resp.json.return_value = {"incidents": [], "pagination": {}}
        mock_request.return_value = mock_resp
        mock_post_finding.return_value = {"finding_id": "f-1", "stored_at": "now"}
        mock_submit_report.return_value = {"report_id": "r-1", "stored_at": "now"}

        # get_logs is normal
        mock_get_logs.return_value = {
            "logs": [{"id": "log-1", "severity": "ERROR", "message": "Connection timeout", "trace_id": "tr-123"}]
        }
        # search_runbooks raises BackendNotFoundError (404)
        mock_search.side_effect = BackendNotFoundError(404, "Runbook Not Found", None)

        graph = get_graph()
        initial_state = {
            "incident_title": "Payment API Latency Spike",
            "incident_summary": "P95 latency exceeded threshold"
        }
        result = graph.invoke(initial_state)

        # Verify workflow completed successfully with degraded RAG
        self.assertEqual(result.get("status"), "completed")
        findings = result.get("findings", [])
        # 4 findings: log_anomaly + degraded_rag + correlation + report
        self.assertEqual(len(findings), 4)

        # Finding 0: normal log_anomaly from log_query_agent
        self.assertEqual(findings[0]["agent"], "log_query_agent")
        self.assertEqual(findings[0]["type"], "log_anomaly")

        # Finding 1: degraded from rag_agent
        self.assertEqual(findings[1]["agent"], "rag_agent")
        self.assertEqual(findings[1]["type"], "degraded")
        self.assertTrue(findings[1]["degraded"])
        self.assertEqual(findings[1]["error_category"], "not_found")
        self.assertEqual(findings[1]["evidence"]["status_code"], 404)
        self.assertEqual(findings[1]["evidence"]["backend"], "mock-go-backend")

        # Verify events include rag degraded entry
        events = result.get("incident_events", [])
        rag_degraded_events = [e for e in events if e.get("source") == "rag_agent" and e.get("event_type") == "degraded"]
        self.assertEqual(len(rag_degraded_events), 1)

    @patch("internal.client.go_backend.GoBackendClient.submit_report")
    @patch("internal.client.go_backend.GoBackendClient.post_finding")
    @patch("internal.client.go_backend.GoBackendClient._request")
    @patch("internal.client.go_backend.GoBackendClient.create_incident")
    @patch("internal.client.go_backend.GoBackendClient.get_services")
    @patch("internal.client.go_backend.GoBackendClient.get_health")
    @patch("internal.client.go_backend.GoBackendClient.get_logs")
    @patch("internal.client.go_backend.GoBackendClient.search_runbooks")
    def test_rag_agent_degradation_server_error(self, mock_search, mock_get_logs,
                                                 mock_health, mock_services,
                                                 mock_create_incident, mock_request,
                                                 mock_post_finding, mock_submit_report):
        # Supervisor mocks
        mock_health.return_value = {"status": "ok", "components": {}}
        mock_services.return_value = {"services": []}
        mock_create_incident.return_value = {"incident_id": "inc-test-004", "status": "open"}
        mock_resp = MagicMock()
        mock_resp.json.return_value = {"incidents": [], "pagination": {}}
        mock_request.return_value = mock_resp
        mock_post_finding.return_value = {"finding_id": "f-1", "stored_at": "now"}
        mock_submit_report.return_value = {"report_id": "r-1", "stored_at": "now"}

        # get_logs is normal
        mock_get_logs.return_value = {
            "logs": [{"id": "log-1", "severity": "ERROR", "message": "Connection timeout", "trace_id": "tr-123"}]
        }
        # search_runbooks raises BackendUnavailableError (500)
        mock_search.side_effect = BackendUnavailableError(500, "Internal Server Error", None)

        graph = get_graph()
        initial_state = {
            "incident_title": "Payment API Latency Spike",
            "incident_summary": "P95 latency exceeded threshold"
        }
        result = graph.invoke(initial_state)

        # Verify workflow completed successfully with degraded RAG
        self.assertEqual(result.get("status"), "completed")
        findings = result.get("findings", [])
        # 4 findings: log_anomaly + degraded_rag + correlation + report
        self.assertEqual(len(findings), 4)

        # Finding 1: degraded from rag_agent
        self.assertEqual(findings[1]["agent"], "rag_agent")
        self.assertEqual(findings[1]["type"], "degraded")
        self.assertTrue(findings[1]["degraded"])
        self.assertEqual(findings[1]["error_category"], "server_error")
        self.assertEqual(findings[1]["evidence"]["status_code"], 500)


if __name__ == "__main__":
    unittest.main()
