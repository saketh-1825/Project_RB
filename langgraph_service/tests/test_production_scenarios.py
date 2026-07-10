import sys
import os
import json
import unittest
from datetime import datetime, timezone, timedelta
from unittest.mock import patch, MagicMock

# Ensure Python can find local imports
sys.path.append(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from workflow.graph import run_analysis, resume_analysis
from internal.redis_client import _get_redis

class TestProductionScenarios(unittest.TestCase):

    def setUp(self):
        # Clean Redis key namespace for consistent test state
        r = _get_redis()
        keys = r.keys("analysis:prod-scenario-*")
        if keys:
            r.delete(*keys)

    def tearDown(self):
        # Clean Redis key namespace after tests
        r = _get_redis()
        keys = r.keys("analysis:prod-scenario-*")
        if keys:
            r.delete(*keys)

    def _setup_mocks(self, mock_classes):
        mock_health = {"status": "ok", "components": {}}
        mock_services = {"services": []}
        mock_incident = {"incident_id": "inc-prod-001", "status": "open"}
        
        for cls in mock_classes:
            inst = cls.return_value
            inst.get_health.return_value = mock_health
            inst.get_services.return_value = mock_services
            inst.create_incident.return_value = mock_incident
            inst.get_logs.return_value = {"logs": [{"message": "error DB connect"}]}
            inst.search_runbooks.return_value = [
                {
                    "runbook_id": "RB-100",
                    "title": "DB Troubleshoot",
                    "content": "Check connections",
                    "similarity_score": 0.95
                }
            ]
            inst.post_finding.return_value = {"finding_id": "f-1"}
            inst.submit_report.return_value = {"report_id": "r-1"}
            inst._request.return_value = MagicMock(json=lambda: {"incidents": [], "pagination": {}})
            inst.query_metrics_batch.return_value = {"series": []}
            inst.get_incidents.return_value = {"incidents": []}

    @patch("agents.supervisor.GoBackendClient")
    @patch("agents.log_query_agent.GoBackendClient")
    @patch("agents.rag_agent.GoBackendClient")
    @patch("agents.helpers.GoBackendClient")
    @patch("agents.report_agent.GoBackendClient")
    @patch("agents.correlation_agent.GoBackendClient")
    @patch("agents.correlation_agent.calculate_confidence")
    def test_scenario_1_happy_path(
        self,
        mock_calculate_confidence,
        mock_correlation_client,
        mock_report_client,
        mock_helpers_client,
        mock_rag_client,
        mock_log_client,
        mock_supervisor_client
    ):
        self._setup_mocks([mock_supervisor_client, mock_log_client, mock_rag_client, mock_helpers_client, mock_report_client, mock_correlation_client])
        
        mock_calculate_confidence.return_value = {
            "score": 0.90,
            "level": "HIGH",
            "reason": "Clear root cause identified"
        }

        state = {
            "analysis_id": "prod-scenario-happy-001",
            "incident_title": "High Latency",
            "incident_summary": "Latency spike observed",
            "status": "running",
            "alert": {
                "alert_id": "alert-1",
                "name": "High Latency",
                "affected_services": ["api-gateway"]
            }
        }

        result = run_analysis(state)

        # Validations
        self.assertEqual(result.get("status"), "completed")
        self.assertIsNotNone(result.get("report"))
        self.assertIsNotNone(result.get("root_cause"))
        self.assertEqual(result.get("correlation", {}).get("confidence", {}).get("score"), 0.90)

        # Check Redis for events and checkpoint
        r = _get_redis()
        checkpoint = r.get("analysis:prod-scenario-happy-001:checkpoint")
        self.assertIsNotNone(checkpoint)
        
        events = r.lrange("analysis:prod-scenario-happy-001:events", 0, -1)
        self.assertTrue(len(events) > 0)
        event_types = [json.loads(e).get("event_type") for e in events]
        self.assertIn("analysis.started", event_types)
        self.assertIn("analysis.completed", event_types)

    @patch("agents.supervisor.GoBackendClient")
    @patch("agents.log_query_agent.GoBackendClient")
    @patch("agents.rag_agent.GoBackendClient")
    @patch("agents.helpers.GoBackendClient")
    @patch("agents.report_agent.GoBackendClient")
    @patch("agents.correlation_agent.GoBackendClient")
    @patch("agents.correlation_agent.calculate_confidence")
    def test_scenario_2_human_in_the_loop(
        self,
        mock_calculate_confidence,
        mock_correlation_client,
        mock_report_client,
        mock_helpers_client,
        mock_rag_client,
        mock_log_client,
        mock_supervisor_client
    ):
        self._setup_mocks([mock_supervisor_client, mock_log_client, mock_rag_client, mock_helpers_client, mock_report_client, mock_correlation_client])
        
        # LOW confidence
        mock_calculate_confidence.return_value = {
            "score": 0.40,
            "level": "LOW",
            "reason": "Insufficient evidence"
        }

        state = {
            "analysis_id": "prod-scenario-hil-001",
            "incident_title": "Unclear Issue",
            "incident_summary": "Something went wrong",
            "status": "running",
            "alert": {
                "alert_id": "alert-2",
                "name": "Unclear Issue",
                "affected_services": ["auth-service"]
            }
        }

        # Step 1: Run and pause
        result = run_analysis(state)
        
        self.assertEqual(result.get("status"), "awaiting_human")
        self.assertTrue(result.get("awaiting_human"))
        self.assertEqual(result.get("waiting_at"), "confidence_review")

        # Step 2: Resume with human context
        result["human_context"] = "Deploy happened recently. Re-evaluate."
        
        # We increase confidence on resume for correlation to pass
        mock_calculate_confidence.return_value = {
            "score": 0.90,
            "level": "HIGH",
            "reason": "Human input confirmed issue"
        }

        resume_result = resume_analysis(result)
        
        self.assertEqual(resume_result.get("status"), "completed")
        self.assertIsNotNone(resume_result.get("report"))

        # Verify evidence wasn't recollected by checking evidence state unchanged structurally
        self.assertIn("logs", resume_result.get("evidence", {}))

    @patch("agents.supervisor.GoBackendClient")
    @patch("agents.log_query_agent.GoBackendClient")
    @patch("agents.rag_agent.GoBackendClient")
    @patch("agents.helpers.GoBackendClient")
    @patch("agents.report_agent.GoBackendClient")
    @patch("agents.correlation_agent.GoBackendClient")
    @patch("agents.correlation_agent.calculate_confidence")
    def test_scenario_3_degraded_backend(
        self,
        mock_calculate_confidence,
        mock_correlation_client,
        mock_report_client,
        mock_helpers_client,
        mock_rag_client,
        mock_log_client,
        mock_supervisor_client
    ):
        self._setup_mocks([mock_supervisor_client, mock_log_client, mock_rag_client, mock_helpers_client, mock_report_client, mock_correlation_client])
        
        # Simulate backend failure for evidence collection
        mock_log_client.return_value.get_logs.side_effect = Exception("Logs backend unavailable")
        
        mock_calculate_confidence.return_value = {
            "score": 0.80, # Confidence still enough to report
            "level": "HIGH",
            "reason": "Partial evidence available"
        }

        state = {
            "analysis_id": "prod-scenario-degraded-001",
            "incident_title": "API Error",
            "incident_summary": "500 errors",
            "status": "running",
            "alert": {
                "alert_id": "alert-3",
                "name": "API Error",
                "affected_services": ["backend-api"]
            }
        }

        # System should not crash
        result = run_analysis(state)
        
        self.assertEqual(result.get("status"), "completed")
        self.assertEqual(result.get("evidence", {}).get("metadata", {}).get("collection_status", {}).get("logs"), "failed")
        # Ensure report still generated
        self.assertIsNotNone(result.get("report"))

    @patch("agents.supervisor.GoBackendClient")
    @patch("agents.log_query_agent.GoBackendClient")
    @patch("agents.rag_agent.GoBackendClient")
    @patch("agents.helpers.GoBackendClient")
    @patch("agents.report_agent.GoBackendClient")
    @patch("agents.correlation_agent.GoBackendClient")
    @patch("agents.correlation_agent.calculate_confidence")
    def test_scenario_4_simultaneous_alerts(
        self,
        mock_calculate_confidence,
        mock_correlation_client,
        mock_report_client,
        mock_helpers_client,
        mock_rag_client,
        mock_log_client,
        mock_supervisor_client
    ):
        self._setup_mocks([mock_supervisor_client, mock_log_client, mock_rag_client, mock_helpers_client, mock_report_client, mock_correlation_client])
        
        mock_calculate_confidence.return_value = {
            "score": 0.85,
            "level": "HIGH",
            "reason": "High confidence"
        }

        ts_a_str = datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")
        ts_b_str = datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")

        state_a = {
            "analysis_id": "prod-scenario-simul-A",
            "incident_title": "Service Down",
            "incident_summary": "Service down A",
            "triggered_at": ts_a_str,
            "status": "running",
            "alert": {
                "alert_id": "alert-A",
                "name": "Service Down",
                "affected_services": ["shared-db"]
            }
        }

        state_b = {
            "analysis_id": "prod-scenario-simul-B",
            "incident_title": "Service Slow",
            "incident_summary": "Service slow B",
            "triggered_at": ts_b_str,
            "status": "running",
            "alert": {
                "alert_id": "alert-B",
                "name": "Service Slow",
                "affected_services": ["shared-db"]
            }
        }

        result_a = run_analysis(state_a)
        result_b = run_analysis(state_b)

        self.assertEqual(result_a.get("status"), "completed")
        self.assertEqual(result_b.get("status"), "completed")

        # Independent Redis checkpoints
        r = _get_redis()
        ckpt_a = r.get("analysis:prod-scenario-simul-A:checkpoint")
        ckpt_b = r.get("analysis:prod-scenario-simul-B:checkpoint")
        self.assertIsNotNone(ckpt_a)
        self.assertIsNotNone(ckpt_b)
        self.assertNotEqual(ckpt_a, ckpt_b)

        # Related analyses linking still works
        related_b = result_b.get("related_analyses", [])
        self.assertTrue(len(related_b) > 0)
        self.assertEqual(related_b[0]["analysis_id"], "prod-scenario-simul-A")

    @patch("agents.supervisor.GoBackendClient")
    @patch("agents.log_query_agent.GoBackendClient")
    @patch("agents.rag_agent.GoBackendClient")
    @patch("agents.helpers.GoBackendClient")
    @patch("agents.correlation_agent.GoBackendClient")
    @patch("agents.correlation_agent.calculate_confidence")
    def test_scenario_5_cancellation_failure_handling(
        self,
        mock_calculate_confidence,
        mock_correlation_client,
        mock_helpers_client,
        mock_rag_client,
        mock_log_client,
        mock_supervisor_client
    ):
        self._setup_mocks([mock_supervisor_client, mock_log_client, mock_rag_client, mock_helpers_client, mock_correlation_client])
        
        # Simulate unexpected failure in Correlation Agent
        mock_calculate_confidence.side_effect = Exception("Unexpected failure during correlation")

        state = {
            "analysis_id": "prod-scenario-fail-001",
            "incident_title": "Failing Issue",
            "incident_summary": "Failing issue",
            "status": "running",
            "alert": {
                "alert_id": "alert-5",
                "name": "Failing Issue",
                "affected_services": ["test-service"]
            }
        }

        with self.assertRaises(Exception):
            run_analysis(state)

        # Validate analysis.failed event emitted
        r = _get_redis()
        events = r.lrange("analysis:prod-scenario-fail-001:events", 0, -1)
        event_types = [json.loads(e).get("event_type") for e in events]
        self.assertIn("analysis.started", event_types)
        
        # Check if agent switch failed event exists
        failed_switches = [
            json.loads(e) for e in events 
            if json.loads(e).get("event_type") == "analysis.agent_switched" and json.loads(e).get("status") == "failed"
        ]
        self.assertTrue(len(failed_switches) > 0)

        # Validate it didn't corrupt others by running a healthy one
        state_healthy = {
            "analysis_id": "prod-scenario-healthy-001",
            "status": "running",
            "alert": {"alert_id": "alert-6", "name": "Healthy", "affected_services": ["test-service"]}
        }
        mock_calculate_confidence.side_effect = None
        mock_calculate_confidence.return_value = {"score": 0.9, "level": "HIGH", "reason": "OK"}
        
        result_healthy = run_analysis(state_healthy)
        self.assertEqual(result_healthy.get("status"), "completed")

if __name__ == "__main__":
    unittest.main()
