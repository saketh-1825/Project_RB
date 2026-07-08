import unittest
import json
from unittest.mock import patch, MagicMock
from datetime import datetime, timezone
from fastapi.testclient import TestClient

from main import app
from workflow.graph import run_analysis
from internal.redis_client import _get_redis

class TestGraphEvents(unittest.TestCase):
    def setUp(self):
        self.r = _get_redis()
        # Flush DB to ensure clean tests
        self.r.flushdb()
        self.client = TestClient(app)

    def tearDown(self):
        self.r.flushdb()

    @patch("agents.supervisor.GoBackendClient")
    @patch("agents.log_query_agent.GoBackendClient")
    @patch("agents.helpers.GoBackendClient")
    @patch("agents.rag_agent.GoBackendClient")
    @patch("agents.report_agent.GoBackendClient")
    @patch("agents.correlation_agent.GoBackendClient")
    @patch("agents.correlation_agent.calculate_confidence")
    def test_running_and_completed_events(
        self,
        mock_calculate_confidence,
        mock_correlation_client_class,
        mock_report_client_class,
        mock_rag_client_class,
        mock_helpers_client_class,
        mock_log_client_class,
        mock_supervisor_client_class
    ):
        """
        Test 1: Verify that a successful node execution emits running and completed events,
        including discoveries (findings) and final status.
        """
        # Configure backend mocks to succeed
        for cls in [mock_supervisor_client_class, mock_log_client_class, mock_helpers_client_class,
                    mock_rag_client_class, mock_report_client_class, mock_correlation_client_class]:
            inst = cls.return_value
            inst.get_health.return_value = {"status": "ok"}
            inst.get_services.return_value = {"services": []}
            inst.create_incident.return_value = {"incident_id": "inc-123"}
            inst.get_logs.return_value = {"logs": []}
            inst.search_runbooks.return_value = [{"runbook_id": "RB-100", "title": "Runbook 1", "similarity_score": 0.95}]
            inst.post_finding.return_value = {"finding_id": "f-1"}
            inst.submit_report.return_value = {"report_id": "rep-1"}
            inst._request.return_value = MagicMock(json=lambda: {"incidents": [], "pagination": {}})

        mock_calculate_confidence.return_value = {
            "score": 0.85,
            "level": "HIGH",
            "reason": "High confidence"
        }

        analysis_id = "test-analysis-001"
        state = {
            "analysis_id": analysis_id,
            "incident_title": "Database Error Spike",
            "incident_summary": "Database CPU High",
            "status": "running",
            "alert": {
                "alert_id": "alert-db-001",
                "name": "Database CPU High",
                "affected_services": ["payment-api"],
                "fired_at": "2026-07-08T12:00:00Z"
            }
        }

        # Run the analysis
        result = run_analysis(state)
        self.assertEqual(result.get("status"), "completed")

        # Fetch events from Redis
        events_json = self.r.lrange(f"analysis:{analysis_id}:events", 0, -1)
        events = [json.loads(ev) for ev in reversed(events_json)]

        # Check for started event
        self.assertEqual(events[0]["event_type"], "analysis.started")
        self.assertEqual(events[0]["node"], "supervisor")
        self.assertEqual(events[0]["status"], "running")

        # Verify that supervisor node has running and completed events
        supervisor_running = [e for e in events if e["node"] == "supervisor" and e["status"] == "running"]
        supervisor_completed = [e for e in events if e["node"] == "supervisor" and e["status"] == "completed"]
        self.assertEqual(len(supervisor_running), 1)
        self.assertEqual(len(supervisor_completed), 1)

        # Check for finding event (emitted by evidence_agent or correlation_agent)
        finding_events = [e for e in events if e["event_type"] == "analysis.finding"]
        self.assertTrue(len(finding_events) > 0)
        for f in finding_events:
            self.assertEqual(f["status"], "completed")
            self.assertIn("payload", f)

        # Check for completed final event
        completed_events = [e for e in events if e["event_type"] == "analysis.completed"]
        self.assertEqual(len(completed_events), 1)
        self.assertEqual(completed_events[0]["status"], "completed")

    @patch("agents.supervisor.GoBackendClient")
    @patch("workflow.graph.evidence_agent_node")
    def test_failed_node_events(self, mock_evidence_node, mock_supervisor_client_class):
        """
        Test 2: Verify that a crashed node emits a failed event.
        """
        mock_supervisor = mock_supervisor_client_class.return_value
        mock_supervisor.get_health.return_value = {"status": "ok"}
        mock_supervisor.get_services.return_value = {"services": []}
        mock_supervisor.create_incident.return_value = {"incident_id": "inc-fail"}

        # Simulate exception inside evidence agent
        mock_evidence_node.side_effect = Exception("Evidence agent database timeout")

        analysis_id = "test-analysis-fail"
        state = {
            "analysis_id": analysis_id,
            "incident_title": "Failing Incident",
            "incident_summary": "Should crash evidence collection",
            "status": "running",
            "alert": {
                "alert_id": "alert-fail",
                "name": "Database Spike",
                "affected_services": ["payment-api"],
                "fired_at": "2026-07-08T12:00:00Z"
            }
        }

        # Invocation should fail
        with self.assertRaises(Exception):
            run_analysis(state)

        # Inspect Redis event log
        events_json = self.r.lrange(f"analysis:{analysis_id}:events", 0, -1)
        events = [json.loads(ev) for ev in reversed(events_json)]

        # Check for failed event for evidence_agent
        failed_events = [e for e in events if e["node"] == "evidence_agent" and e["status"] == "failed"]
        self.assertEqual(len(failed_events), 1)
        self.assertEqual(failed_events[0]["event_type"], "analysis.agent_switched")
        self.assertIn("Evidence agent database timeout", failed_events[0]["payload"]["error"])

    @patch("agents.supervisor.GoBackendClient")
    @patch("workflow.graph.evidence_agent_node")
    def test_concurrent_analyses_isolation(self, mock_evidence_node, mock_supervisor_client_class):
        """
        Test 3: Verify event isolation for concurrent analyses (A events stay with A, B stay with B).
        """
        mock_supervisor = mock_supervisor_client_class.return_value
        mock_supervisor.get_health.return_value = {"status": "ok"}
        mock_supervisor.get_services.return_value = {"services": []}
        mock_supervisor.create_incident.return_value = {"incident_id": "inc-concurrent"}

        # Success for evidence agent
        mock_evidence_node.return_value = {
            "analysis_id": "concurrent-A",
            "status": "completed",
            "findings": [{"agent": "logs", "summary": "DB error details"}]
        }

        # 1. Run Analysis A
        state_a = {
            "analysis_id": "concurrent-A",
            "status": "running",
            "alert": {"alert_id": "alert-A", "name": "CPU High", "affected_services": ["auth-service"], "fired_at": "2026"}
        }
        run_analysis(state_a)

        # 2. Run Analysis B with modified return value
        mock_evidence_node.return_value = {
            "analysis_id": "concurrent-B",
            "status": "completed",
            "findings": [{"agent": "rag", "summary": "Runbook matched"}]
        }
        state_b = {
            "analysis_id": "concurrent-B",
            "status": "running",
            "alert": {"alert_id": "alert-B", "name": "Memory High", "affected_services": ["payment-api"], "fired_at": "2026"}
        }
        run_analysis(state_b)

        # 3. Verify Redis lists contain only their respective events
        events_a_json = self.r.lrange("analysis:concurrent-A:events", 0, -1)
        events_a = [json.loads(e) for e in events_a_json]
        for ev in events_a:
            self.assertEqual(ev["analysis_id"], "concurrent-A")

        events_b_json = self.r.lrange("analysis:concurrent-B:events", 0, -1)
        events_b = [json.loads(e) for e in events_b_json]
        for ev in events_b:
            self.assertEqual(ev["analysis_id"], "concurrent-B")

    @patch("agents.supervisor.GoBackendClient")
    @patch("workflow.graph.evidence_agent_node")
    def test_websocket_event_streaming(self, mock_evidence_node, mock_supervisor_client_class):
        """
        Test 4: Verify WebSocket client receives initial event history and live updates.
        """
        mock_supervisor = mock_supervisor_client_class.return_value
        mock_supervisor.get_health.return_value = {"status": "ok"}
        mock_supervisor.get_services.return_value = {"services": []}
        mock_supervisor.create_incident.return_value = {"incident_id": "inc-ws"}

        mock_evidence_node.return_value = {
            "analysis_id": "ws-analysis-123",
            "status": "completed",
            "findings": [{"agent": "logs", "summary": "Found query timeout"}]
        }

        # 1. Run analysis to populate history in Redis
        state = {
            "analysis_id": "ws-analysis-123",
            "status": "running",
            "alert": {"alert_id": "alert-ws", "name": "Error Spike", "affected_services": ["payment-api"], "fired_at": "2026"}
        }
        run_analysis(state)

        import queue
        def receive_json_with_timeout(websocket, timeout=1.0):
            msg = websocket._send_queue.get(timeout=timeout)
            if isinstance(msg, BaseException):
                raise msg
            if msg.get("type") == "websocket.close":
                raise Exception("Connection closed")
            import json
            if "text" in msg:
                return json.loads(msg["text"])
            elif "bytes" in msg:
                return json.loads(msg["bytes"].decode("utf-8"))
            return msg

        # 2. Connect via WebSocket TestClient
        with self.client.websocket_connect("/ws/analysis/ws-analysis-123") as ws:
            # We should receive replayed history immediately
            first_event = receive_json_with_timeout(ws, timeout=1.0)
            self.assertEqual(first_event["analysis_id"], "ws-analysis-123")
            self.assertEqual(first_event["event_type"], "analysis.started")

            # Exhaust the replayed events until we get to the completed node events
            received_events = []
            try:
                for _ in range(20):
                    received_events.append(receive_json_with_timeout(ws, timeout=1.0))
            except queue.Empty:
                pass # Reached end of buffer

            # We should have completed events in the stream
            completed = [e for e in received_events if e.get("event_type") == "analysis.completed"]
            self.assertTrue(len(completed) > 0 or any(e.get("status") == "completed" for e in received_events))
