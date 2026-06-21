"""
Unit tests for GoBackendClient against WireMock.

These tests hit the live WireMock service (mock-go-backend) at http://localhost:8080.
To run:
    1. Start the AI dev environment: bash scripts/dev_ai.sh
    2. Wait for WireMock to be ready on :8080
    3. Run:  python -m pytest test_client_wiremock.py -v
"""
import sys
import os
import unittest

# Ensure python can find local imports
sys.path.append(os.path.dirname(os.path.abspath(__file__)))

from internal.client.go_backend import GoBackendClient


WIREMOCK_BASE_URL = os.environ.get("WIREMOCK_URL", "http://localhost:8080")
TEST_TOKEN = "mock-token"


def get_client():
    return GoBackendClient(base_url=WIREMOCK_BASE_URL, token=TEST_TOKEN)


class TestGoBackendClientAgainstWireMock(unittest.TestCase):
    """
    Tests each typed method of GoBackendClient against WireMock stubs
    defined in mocks/go-backend/mappings/*.json.
    """

    @classmethod
    def setUpClass(cls):
        cls.client = get_client()

    @classmethod
    def tearDownClass(cls):
        cls.client.close()

    # ---------------------------------------------------
    # HEALTH
    # ---------------------------------------------------

    def test_get_health(self):
        """GET /api/v1/health → status=ok, components dict"""
        result = self.client.get_health()
        self.assertEqual(result["status"], "ok")
        self.assertIn("components", result)
        components = result["components"]
        self.assertEqual(components["log_store"], "ok")
        self.assertEqual(components["metric_store"], "ok")
        self.assertEqual(components["redis"], "ok")
        self.assertEqual(components["vector_index"], "ok")
        self.assertIn("uptime_seconds", result)

    # ---------------------------------------------------
    # SERVICES
    # ---------------------------------------------------

    def test_get_services(self):
        """GET /api/v1/services → 5 service nodes with topology"""
        result = self.client.get_services()
        self.assertIn("services", result)
        services = result["services"]
        self.assertEqual(len(services), 5)

        # Check service names
        service_names = [s["name"] for s in services]
        self.assertIn("api-gateway", service_names)
        self.assertIn("payment-api", service_names)
        self.assertIn("order-service", service_names)

        # Check payment-api is down
        payment_api = [s for s in services if s["name"] == "payment-api"][0]
        self.assertEqual(payment_api["health"], "down")

    # ---------------------------------------------------
    # LOGS
    # ---------------------------------------------------

    def test_get_logs(self):
        """GET /api/v1/logs → returns log entries with trace_ids"""
        result = self.client.get_logs(
            from_time="2026-03-29T09:50:00Z",
            to_time="2026-03-29T10:05:00Z",
            services=["payment-api"],
            levels=["ERROR", "FATAL"]
        )
        self.assertIn("logs", result)
        logs = result["logs"]
        self.assertGreaterEqual(len(logs), 1)

        # Verify log structure
        first_log = logs[0]
        self.assertIn("id", first_log)
        self.assertIn("level", first_log)
        self.assertIn("service", first_log)
        self.assertIn("message", first_log)

    def test_get_log_anomalies(self):
        """GET /api/v1/logs/anomalies → anomalous windows"""
        result = self.client.get_log_anomalies(
            from_time="2026-03-29T09:50:00Z",
            to_time="2026-03-29T10:05:00Z",
            services=["payment-api"]
        )
        self.assertIn("anomalous_windows", result)
        windows = result["anomalous_windows"]
        self.assertGreaterEqual(len(windows), 1)

        first_window = windows[0]
        self.assertIn("window_start", first_window)
        self.assertIn("window_end", first_window)
        self.assertIn("spike_factor", first_window)

    # ---------------------------------------------------
    # TRACES
    # ---------------------------------------------------

    def test_get_trace(self):
        """GET /api/v1/traces/trace-abc123 → trace with spans"""
        result = self.client.get_trace("trace-abc123")
        self.assertEqual(result["trace_id"], "trace-abc123")
        self.assertEqual(result["root_service"], "api-gateway")
        self.assertIn("spans", result)
        self.assertEqual(len(result["spans"]), 4)

        # Verify the deepest span shows the DB issue
        db_span = [s for s in result["spans"] if "db.query" in s.get("operation", "")]
        self.assertEqual(len(db_span), 1)
        self.assertIn("connection pool exhausted", db_span[0]["error_message"])

    # ---------------------------------------------------
    # RUNBOOKS
    # ---------------------------------------------------

    def test_search_runbooks(self):
        """GET /api/v1/runbooks/search → runbook results with scores"""
        result = self.client.search_runbooks("database connection pool exhaustion")
        self.assertIsNotNone(result)

        # WireMock returns the result wrapped in {"runbooks": [...]}
        if isinstance(result, dict):
            runbooks = result.get("runbooks", [])
        else:
            runbooks = result

        self.assertGreaterEqual(len(runbooks), 1)
        top = runbooks[0]
        self.assertEqual(top["runbook_id"], "rb-001")
        self.assertIn("similarity_score", top)
        self.assertGreaterEqual(top["similarity_score"], 0.9)

    def test_get_runbooks(self):
        """GET /api/v1/runbooks → list with pagination"""
        result = self.client.get_runbooks()
        self.assertIn("runbooks", result)
        self.assertIn("pagination", result)
        self.assertEqual(len(result["runbooks"]), 2)

    # ---------------------------------------------------
    # INCIDENTS
    # ---------------------------------------------------

    def test_create_incident(self):
        """POST /api/v1/incidents → 201 with incident_id"""
        data = {
            "alert_id": "alert-001",
            "title": "Test Incident",
            "severity": "critical",
            "affected_services": ["payment-api"],
            "opened_by": "test_agent"
        }
        result = self.client.create_incident(data)
        self.assertIn("incident_id", result)
        self.assertIn("status", result)
        self.assertEqual(result["status"], "open")

    def test_get_incident(self):
        """GET /api/v1/incidents/:id → incident details"""
        result = self.client.get_incident("inc-mock-001")
        self.assertIn("incident_id", result)
        self.assertEqual(result["severity"], "critical")
        self.assertEqual(result["status"], "open")
        self.assertIn("affected_services", result)

    def test_post_finding(self):
        """POST /api/v1/incidents/:id/events → 201 with finding_id"""
        finding = {
            "agent": "log_query_agent",
            "type": "log_anomaly",
            "severity": "high",
            "title": "Error spike detected",
            "summary": "Large number of ERROR logs found",
            "confidence": 0.9
        }
        result = self.client.post_finding("inc-mock-001", finding)
        self.assertIn("finding_id", result)
        self.assertIn("stored_at", result)

    def test_submit_report(self):
        """POST /api/v1/incidents/:id/report → 201 with report_id"""
        report = {
            "incident_id": "inc-mock-001",
            "alert_id": "alert-001",
            "title": "Test Report",
            "executive_summary": "Test summary",
            "root_cause": {"description": "Test", "confidence": 0.8},
            "timeline": [],
            "suggested_fixes": [],
        }
        result = self.client.submit_report("inc-mock-001", report)
        self.assertIn("report_id", result)
        self.assertIn("stored_at", result)


if __name__ == "__main__":
    unittest.main()
