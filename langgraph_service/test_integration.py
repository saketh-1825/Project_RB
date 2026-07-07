"""
Integration test: Full graph traversal via POST /api/v1/analyses.

Tests that the graph traverses all 5 nodes:
  supervisor → log_query_agent → rag_agent → correlation_agent → report_agent

Uses unittest.mock to patch GoBackendClient methods with WireMock-equivalent data.
Does NOT require WireMock running — fully self-contained.

Run:  python -m pytest test_integration.py -v
"""
import sys
import os
import unittest
from unittest.mock import patch, MagicMock

# Ensure python can find local imports
sys.path.append(os.path.dirname(os.path.abspath(__file__)))

from internal.errors import GoBackendError
from workflow.graph import get_graph, run_analysis


# ── Mock response data (matches WireMock stubs) ──────────────────────

MOCK_HEALTH = {
    "status": "ok",
    "components": {
        "log_store": "ok",
        "metric_store": "ok",
        "redis": "ok",
        "vector_index": "ok"
    },
    "uptime_seconds": 3600
}

MOCK_SERVICES = {
    "generated_at": "2026-03-29T10:05:00.000Z",
    "services": [
        {
            "service_id": "svc-payment-api",
            "name": "payment-api",
            "health": "down",
            "version": "v2.1.4",
            "dependencies": [],
            "tags": {"team": "payments"}
        },
        {
            "service_id": "svc-order-service",
            "name": "order-service",
            "health": "degraded",
            "version": "v1.8.0",
            "dependencies": [],
            "tags": {"team": "commerce"}
        }
    ]
}

MOCK_INCIDENT_CREATED = {
    "incident_id": "inc-integration-001",
    "title": "Incident opened by LangGraph agent",
    "status": "open",
    "opened_at": "2026-03-29T10:05:00.000Z"
}

MOCK_LOGS = {
    "logs": [
        {
            "id": "log-001",
            "timestamp": "2026-03-29T10:04:55.123Z",
            "level": "ERROR",
            "service": "payment-api",
            "host": "payment-api-pod-7d9f8b-xkp2q",
            "message": "Failed to connect to PostgreSQL",
            "trace_id": "trace-abc123",
            "span_id": "span-001",
            "attributes": {}
        }
    ],
    "total_matched": 1,
    "next_cursor": None,
    "query_duration_ms": 42
}

MOCK_ANOMALIES = {
    "anomalous_windows": [
        {
            "window_start": "2026-03-29T10:04:50.000Z",
            "window_end": "2026-03-29T10:05:00.000Z",
            "service": "payment-api",
            "error_rate": 0.82,
            "baseline_rate": 0.04,
            "spike_factor": 20.5,
            "sample_log_ids": ["log-001"]
        }
    ]
}

MOCK_TRACE = {
    "trace_id": "trace-abc123",
    "root_service": "api-gateway",
    "total_duration_ms": 5123,
    "spans": [
        {
            "trace_id": "trace-abc123",
            "span_id": "span-001",
            "service": "payment-api",
            "operation": "POST /v1/charge",
            "duration_ms": 5000,
            "status": "timeout",
            "error_message": "context deadline exceeded"
        }
    ]
}

MOCK_RUNBOOK_SEARCH = {
    "runbooks": [
        {
            "runbook_id": "rb-001",
            "title": "PostgreSQL Connection Pool Exhaustion",
            "content": "Check pg_stat_activity for long-running queries",
            "similarity_score": 0.96,
            "last_updated": "2026-01-15T08:00:00.000Z"
        }
    ]
}

MOCK_INCIDENTS_LIST = {
    "incidents": [
        {
            "incident_id": "inc-past-001",
            "title": "Payment API DB pool exhaustion (Jan 2026)",
            "severity": "critical",
            "status": "resolved",
            "affected_services": ["payment-api", "order-service"],
            "opened_at": "2026-01-20T14:22:00.000Z",
            "resolved_at": "2026-01-20T14:55:00.000Z",
            "root_cause_summary": "PostgreSQL primary ran out of connection slots."
        }
    ],
    "pagination": {"page": 1, "page_size": 20, "total": 1}
}

MOCK_FINDING_POSTED = {
    "finding_id": "finding-integration-001",
    "stored_at": "2026-03-29T10:05:01.000Z"
}

MOCK_REPORT_POSTED = {
    "report_id": "report-integration-001",
    "stored_at": "2026-03-29T10:05:02.000Z"
}


class TestFullGraphTraversal(unittest.TestCase):
    """
    Verifies the full 5-node graph traversal with mocked backend.
    """

    @patch("internal.client.go_backend.GoBackendClient.submit_report")
    @patch("internal.client.go_backend.GoBackendClient.post_finding")
    @patch("internal.client.go_backend.GoBackendClient.search_runbooks")
    @patch("internal.client.go_backend.GoBackendClient.get_trace")
    @patch("internal.client.go_backend.GoBackendClient.get_log_anomalies")
    @patch("internal.client.go_backend.GoBackendClient.get_logs")
    @patch("internal.client.go_backend.GoBackendClient.create_incident")
    @patch("internal.client.go_backend.GoBackendClient.get_services")
    @patch("internal.client.go_backend.GoBackendClient.get_health")
    @patch("internal.client.go_backend.GoBackendClient._request")
    def test_full_analysis_traversal(
        self,
        mock_request,
        mock_health,
        mock_services,
        mock_create_incident,
        mock_get_logs,
        mock_get_anomalies,
        mock_get_trace,
        mock_search_runbooks,
        mock_post_finding,
        mock_submit_report,
    ):
        # Configure mocks
        mock_health.return_value = MOCK_HEALTH
        mock_services.return_value = MOCK_SERVICES
        mock_create_incident.return_value = MOCK_INCIDENT_CREATED
        mock_get_logs.return_value = MOCK_LOGS
        mock_get_anomalies.return_value = MOCK_ANOMALIES
        mock_get_trace.return_value = MOCK_TRACE
        mock_search_runbooks.return_value = MOCK_RUNBOOK_SEARCH
        mock_post_finding.return_value = MOCK_FINDING_POSTED
        mock_submit_report.return_value = MOCK_REPORT_POSTED

        # For correlation agent's calls: GET /incidents and POST /metrics/query/batch
        def mock_request_side_effect(method, path, **kwargs):
            resp = MagicMock()
            if "/metrics/query/batch" in path:
                resp.json.return_value = {
                    "series": [
                        {
                            "metric_name": "http_error_rate",
                            "unit": "ratio",
                            "data_points": [{"timestamp": "2026-03-29T10:04:00.000Z", "value": 0.85, "labels": {"service": "payment-api"}}]
                        }
                    ]
                }
            else:
                resp.json.return_value = MOCK_INCIDENTS_LIST
            return resp
        mock_request.side_effect = mock_request_side_effect

        # Build initial state from a realistic alert payload
        initial_state = {
            "analysis_id": "integration-test-001",
            "alert_id": "alert-001",
            "alert": {
                "alert_id": "alert-001",
                "source": "prometheus",
                "name": "HighErrorRate",
                "severity": "critical",
                "status": "firing",
                "fired_at": "2026-03-29T10:00:00.000Z",
                "affected_services": ["payment-api", "order-service"],
                "annotations": {
                    "description": "Error rate > 5% for 5 minutes on payment-api"
                }
            },
            "incident_id": None,
            "findings": [],
            "current_agent": "supervisor",
            "status": "pending",
            "report": None
        }

        graph = get_graph()
        result = graph.invoke(initial_state)

        # ── Verify final status ──
        self.assertEqual(result.get("status"), "completed")

        # ── Verify supervisor set incident_id ──
        self.assertEqual(result.get("incident_id"), "inc-integration-001")
        mock_health.assert_called_once()
        mock_services.assert_called_once()
        mock_create_incident.assert_called_once()

        # ── Verify services topology was loaded ──
        self.assertIsNotNone(result.get("services_topology"))
        self.assertEqual(len(result["services_topology"]["services"]), 2)

        # ── Verify findings from all agents ──
        findings = result.get("findings", [])
        agents_in_findings = [f.get("agent") for f in findings]
        self.assertIn("log_query_agent", agents_in_findings)
        self.assertIn("rag_agent", agents_in_findings)
        self.assertIn("correlation_agent", agents_in_findings)
        self.assertIn("report_agent", agents_in_findings)

        # ── Verify log_query_agent produced a log_anomaly finding ──
        log_findings = [f for f in findings if f.get("agent") == "log_query_agent"]
        self.assertEqual(len(log_findings), 1)
        self.assertEqual(log_findings[0]["type"], "log_anomaly")

        # ── Verify rag_agent produced a runbook finding ──
        rag_findings = [f for f in findings if f.get("agent") == "rag_agent"]
        self.assertEqual(len(rag_findings), 1)
        self.assertEqual(rag_findings[0]["type"], "runbook")
        self.assertEqual(rag_findings[0]["runbook_id"], "rb-001")

        # ── Verify correlation_agent produced correlation ──
        corr_findings = [f for f in findings if f.get("agent") == "correlation_agent"]
        self.assertEqual(len(corr_findings), 1)
        self.assertEqual(corr_findings[0]["type"], "historical_correlation")
        self.assertIsNotNone(result.get("correlation"))

        # ── Verify report_agent produced a report ──
        report = result.get("report")
        self.assertIsNotNone(report)
        self.assertIn("executive_summary", report)
        self.assertIn("root_cause", report)
        self.assertIn("timeline", report)
        self.assertIn("suggested_fixes", report)
        self.assertEqual(report["incident_id"], "inc-integration-001")

        # ── Verify post_finding was called (log + rag + correlation) ──
        self.assertGreaterEqual(mock_post_finding.call_count, 3)

        # ── Verify submit_report was called once ──
        mock_submit_report.assert_called_once()

        # ── Verify incident events timeline ──
        events = result.get("incident_events", [])
        event_sources = [e.get("source") for e in events]
        self.assertIn("log_query_agent", event_sources)
        self.assertIn("rag_agent", event_sources)
        self.assertIn("correlation_agent", event_sources)
        self.assertIn("report_agent", event_sources)


class TestGraphWithDegradedBackend(unittest.TestCase):
    """
    Verifies the graph completes even when the backend is unavailable.
    """

    @patch("internal.client.go_backend.GoBackendClient.submit_report")
    @patch("internal.client.go_backend.GoBackendClient.post_finding")
    @patch("internal.client.go_backend.GoBackendClient.search_runbooks")
    @patch("internal.client.go_backend.GoBackendClient.get_trace")
    @patch("internal.client.go_backend.GoBackendClient.get_log_anomalies")
    @patch("internal.client.go_backend.GoBackendClient.get_logs")
    @patch("internal.client.go_backend.GoBackendClient.create_incident")
    @patch("internal.client.go_backend.GoBackendClient.get_services")
    @patch("internal.client.go_backend.GoBackendClient.get_health")
    @patch("internal.client.go_backend.GoBackendClient._request")
    def test_full_analysis_with_all_backends_down(
        self,
        mock_request,
        mock_health,
        mock_services,
        mock_create_incident,
        mock_get_logs,
        mock_get_anomalies,
        mock_get_trace,
        mock_search_runbooks,
        mock_post_finding,
        mock_submit_report,
    ):
        # All backend calls fail
        backend_error = GoBackendError(503, "Service Unavailable", None)
        mock_health.side_effect = backend_error
        mock_services.side_effect = backend_error
        mock_create_incident.side_effect = backend_error
        mock_get_logs.side_effect = backend_error
        mock_get_anomalies.side_effect = backend_error
        mock_get_trace.side_effect = backend_error
        mock_search_runbooks.side_effect = backend_error
        mock_post_finding.side_effect = backend_error
        mock_submit_report.side_effect = backend_error
        mock_request.side_effect = backend_error

        initial_state = {
            "analysis_id": "degraded-test-001",
            "alert": {
                "name": "HighErrorRate",
                "severity": "critical",
                "affected_services": ["payment-api"],
                "fired_at": "2026-03-29T10:00:00.000Z"
            },
            "incident_id": None,
            "findings": [],
            "current_agent": "supervisor",
            "status": "pending",
            "report": None
        }

        graph = get_graph()
        result = graph.invoke(initial_state)

        # Graph should still complete
        self.assertEqual(result.get("status"), "completed")

        # incident_id will be None since create_incident failed
        self.assertIsNone(result.get("incident_id"))

        # Should still have findings (degraded findings)
        findings = result.get("findings", [])
        self.assertGreater(len(findings), 0)

        # Report should still be generated
        self.assertIsNotNone(result.get("report"))


if __name__ == "__main__":
    unittest.main()
