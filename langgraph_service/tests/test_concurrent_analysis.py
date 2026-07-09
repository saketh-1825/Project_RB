import sys
import os
import json
import unittest
from datetime import datetime, timezone, timedelta
from unittest.mock import patch, MagicMock

# Ensure Python can find local imports
sys.path.append(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from workflow.graph import run_analysis
from internal.redis_client import _get_redis

class TestConcurrentAnalysis(unittest.TestCase):

    def setUp(self):
        # Clean Redis key namespace for consistent test state
        r = _get_redis()
        keys = r.keys("analysis:analysis-concurrent-*")
        if keys:
            r.delete(*keys)

    def tearDown(self):
        # Clean Redis key namespace after tests
        r = _get_redis()
        keys = r.keys("analysis:analysis-concurrent-*")
        if keys:
            r.delete(*keys)

    @patch("agents.supervisor.GoBackendClient")
    @patch("agents.log_query_agent.GoBackendClient")
    @patch("agents.helpers.GoBackendClient")
    @patch("agents.rag_agent.GoBackendClient")
    @patch("agents.report_agent.GoBackendClient")
    @patch("agents.correlation_agent.GoBackendClient")
    @patch("agents.correlation_agent.calculate_confidence")
    def test_concurrent_alert_association(
        self,
        mock_calculate_confidence,
        mock_correlation_client_class,
        mock_report_client_class,
        mock_rag_client_class,
        mock_helpers_client_class,
        mock_log_client_class,
        mock_supervisor_client_class
    ):
        # 1. Setup mock returns for SRE backend clients
        mock_health = {"status": "ok", "components": {}}
        mock_services = {"services": []}
        mock_incident = {"incident_id": "inc-cc-001", "status": "open"}
        
        # Configure instances
        for cls in [mock_supervisor_client_class, mock_log_client_class, mock_helpers_client_class, 
                    mock_rag_client_class, mock_report_client_class, mock_correlation_client_class]:
            inst = cls.return_value
            inst.get_health.return_value = mock_health
            inst.get_services.return_value = mock_services
            inst.create_incident.return_value = mock_incident
            inst.get_logs.return_value = {"logs": []}
            inst.search_runbooks.return_value = [
                {
                    "runbook_id": "RB-100",
                    "title": "Payment Latency Troubleshooting",
                    "content": "Investigate DB connections and cache misses",
                    "similarity_score": 0.95
                }
            ]
            inst.post_finding.return_value = {"finding_id": "f-1"}
            inst.submit_report.return_value = {"report_id": "r-1"}
            inst._request.return_value = MagicMock(json=lambda: {"incidents": [], "pagination": {}})
            inst.query_metrics_batch.return_value = {"series": []}
            inst.get_incidents.return_value = {"incidents": []}
            
        mock_calculate_confidence.return_value = {
            "score": 0.85,
            "level": "HIGH",
            "reason": "High confidence correlation"
        }

        now_ts = datetime.now(timezone.utc)
        ts_a_str = now_ts.isoformat().replace("+00:00", "Z")
        ts_b_str = (now_ts + timedelta(minutes=5)).isoformat().replace("+00:00", "Z")

        # 2. Trigger first alert / analysis (running)
        state_a = {
            "analysis_id": "analysis-concurrent-001",
            "incident_title": "Database Spike A",
            "incident_summary": "High CPU usage on database",
            "triggered_at": ts_a_str,
            "status": "running",
            "alert": {
                "alert_id": "alert-db-001",
                "name": "Database CPU High",
                "affected_services": ["payment-api"],
                "fired_at": ts_a_str
            }
        }
        
        result_a = run_analysis(state_a)
        self.assertEqual(result_a.get("status"), "completed")
        self.assertFalse(result_a.get("related_analyses"))


        # 3. Trigger second alert / analysis (concurrently, within 5 minutes)
        state_b = {
            "analysis_id": "analysis-concurrent-002",
            "incident_title": "Database Spike B",
            "incident_summary": "High CPU usage on database",
            "triggered_at": ts_b_str,
            "status": "running",
            "alert": {
                "alert_id": "alert-db-002",
                "name": "Database CPU High",
                "affected_services": ["payment-api"],
                "fired_at": ts_b_str
            }
        }

        
        result_b = run_analysis(state_b)
        self.assertEqual(result_b.get("status"), "completed")
        
        # Verify A and B are linked in B's state metadata list
        related = result_b.get("related_analyses", [])
        self.assertEqual(len(related), 1)
        self.assertEqual(related[0]["analysis_id"], "analysis-concurrent-001")
        self.assertEqual(related[0]["service"], "payment-api")
        self.assertEqual(related[0]["relationship"], "same_service")
        
        # Verify evidence remains independent (No copying or sharing mutable evidence)
        self.assertIsNone(result_a.get("related_analyses"))
        self.assertNotEqual(id(result_a.get("evidence")), id(result_b.get("evidence")))

if __name__ == "__main__":
    unittest.main()
