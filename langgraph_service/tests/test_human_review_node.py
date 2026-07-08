import sys
import os
import json
import unittest
from unittest.mock import patch, MagicMock

# Ensure Python can find local imports
sys.path.append(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from workflow.graph import run_analysis, resume_analysis
from internal.redis_client import get_analysis_state, _get_redis

class TestHumanReviewNode(unittest.TestCase):

    @patch("agents.supervisor.GoBackendClient")
    @patch("agents.log_query_agent.GoBackendClient")
    @patch("agents.helpers.GoBackendClient")
    @patch("agents.rag_agent.GoBackendClient")
    @patch("agents.report_agent.GoBackendClient")
    @patch("agents.correlation_agent.GoBackendClient")
    @patch("agents.correlation_agent.calculate_confidence")
    def test_human_review_flow(
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
        mock_incident = {"incident_id": "inc-hr-001", "status": "open"}
        
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
            
        # 2. Configure calculate_confidence to return LOW confidence first
        mock_calculate_confidence.return_value = {
            "score": 0.3,
            "level": "LOW",
            "reason": "Confidence is low because of lack of logs."
        }
        
        initial_state = {
            "analysis_id": "analysis-hr-test-001",
            "incident_title": "HR Latency Spike",
            "incident_summary": "P95 latency exceeded threshold on payments-api"
        }
        
        # 3. Run initial analysis
        result = run_analysis(initial_state)
        
        # Verify it halted at human review
        self.assertEqual(result.get("status"), "awaiting_human")
        self.assertEqual(result.get("waiting_at"), "confidence_review")
        self.assertEqual(result.get("review_reason"), "Confidence is low because of lack of logs.")
        self.assertTrue(result.get("requires_input"))
        self.assertTrue(result.get("awaiting_human"))
        
        # 4. Verify Redis isolated checkpoint is persisted
        r = _get_redis()
        checkpoint_val = r.get("analysis:analysis-hr-test-001:checkpoint")
        self.assertIsNotNone(checkpoint_val)
        
        checkpoint_state = json.loads(checkpoint_val)
        self.assertEqual(checkpoint_state.get("status"), "awaiting_human")
        self.assertEqual(checkpoint_state.get("waiting_at"), "confidence_review")
        
        # 5. Configure calculate_confidence to return HIGH confidence for the resume phase
        mock_calculate_confidence.return_value = {
            "score": 0.85,
            "level": "HIGH",
            "reason": "Confidence boosted by operator runbook verification."
        }
        
        # Update state with human context
        resume_payload = dict(result)
        resume_payload["human_context"] = "Operator confirmed database is healthy."
        
        # 6. Resume analysis
        resumed_result = resume_analysis(resume_payload)
        
        # Verify it completed successfully
        self.assertEqual(resumed_result.get("status"), "completed")
        self.assertFalse(resumed_result.get("awaiting_human"))
        self.assertIsNone(resumed_result.get("waiting_at"))
        
        # Check that checkpoint state updated
        final_checkpoint_val = r.get("analysis:analysis-hr-test-001:checkpoint")
        self.assertIsNotNone(final_checkpoint_val)
        final_checkpoint_state = json.loads(final_checkpoint_val)
        self.assertEqual(final_checkpoint_state.get("status"), "completed")
        
if __name__ == "__main__":
    unittest.main()
