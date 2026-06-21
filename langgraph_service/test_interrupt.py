import sys
import os
import json
import unittest
from unittest.mock import MagicMock

# Add current directory to path
sys.path.append(os.path.dirname(os.path.abspath(__file__)))

# Mock search_runbooks on GoBackendClient before importing routes/app
from internal.client.go_backend import GoBackendClient

def mock_search_runbooks(self, query: str, *args, **kwargs):
    print(f"[MOCK] search_runbooks called with query: '{query}'")
    # If the human context is injected, return high similarity score
    if "deployment 245" in query.lower() or "deployment 245" in query:
        return [
            {
                "runbook_id": "RB-100",
                "title": "Payment Latency Troubleshooting",
                "content": "Investigate DB connections and cache misses",
                "similarity_score": 0.92
            }
        ]
    # Otherwise, return low similarity score
    return [
        {
            "runbook_id": "RB-100",
            "title": "Payment Latency Troubleshooting",
            "content": "Investigate DB connections and cache misses",
            "similarity_score": 0.42
        }
    ]

def mock_get_health(self):
    return {"status": "ok", "components": {}, "uptime_seconds": 3600}

def mock_get_services(self):
    return {"services": [], "generated_at": "2026-03-29T10:05:00.000Z"}

def mock_create_incident(self, data):
    return {"incident_id": "inc-interrupt-001", "title": "Test", "status": "open", "opened_at": "now"}

def mock_post_finding(self, incident_id, finding):
    return {"finding_id": "f-mock", "stored_at": "now"}

def mock_submit_report(self, incident_id, report):
    return {"report_id": "r-mock", "stored_at": "now"}

_original_request = GoBackendClient._request
def mock_request_for_incidents(self, method, path, **kwargs):
    """Mock _request to return empty incidents list for correlation agent"""
    if "/api/v1/incidents" in path and method == "GET":
        resp = MagicMock()
        resp.json.return_value = {"incidents": [], "pagination": {}}
        return resp
    return _original_request(self, method, path, **kwargs)

GoBackendClient.search_runbooks = mock_search_runbooks
GoBackendClient.get_health = mock_get_health
GoBackendClient.get_services = mock_get_services
GoBackendClient.create_incident = mock_create_incident
GoBackendClient.post_finding = mock_post_finding
GoBackendClient.submit_report = mock_submit_report
GoBackendClient._request = mock_request_for_incidents

# Import FastAPI test client
from fastapi.testclient import TestClient
from main import app
from workflow.graph import get_graph, run_analysis
from internal.redis_client import get_analysis_state

client = TestClient(app)

class TestHumanInterrupt(unittest.TestCase):
    def test_human_interrupt_flow(self):
        print("\n--- Test Phase 1: Start Analysis (expects pause) ---")
        graph = get_graph()
        
        # 1. Trigger initial graph invoke
        initial_state = {
            "analysis_id": "test_interrupt_analysis_001",
            "incident_title": "Payment API Latency Spike",
            "incident_summary": "P95 latency exceeded threshold"
        }
        
        result = run_analysis(initial_state)
        
        # Verify result is paused and awaiting human input
        self.assertEqual(result.get("status"), "awaiting_human")
        self.assertTrue(result.get("awaiting_human"))
        self.assertEqual(result.get("waiting_at"), "rag_agent")
        self.assertIsNotNone(result.get("interrupt_question"))
        self.assertEqual(result.get("resume_count"), 0)
        self.assertIsNone(result.get("last_interrupted_at"))
        
        # Check Redis persistence
        redis_state = get_analysis_state("test_interrupt_analysis_001")
        self.assertIsNotNone(redis_state)
        self.assertEqual(redis_state.get("status"), "awaiting_human")
        self.assertTrue(redis_state.get("awaiting_human"))
        
        print("✅ Phase 1 passed successfully! Graph paused and state persisted.")

        print("\n--- Test Phase 2: Post Interrupt (expects resume and completion) ---")
        # 2. Simulate POST /api/v1/analyses/{analysis_id}/interrupt
        payload = {
            "interrupt_type": "provide_context",
            "payload": {
                "message": "Issue started after deployment 245"
            },
            "provided_by": "operator"
        }
        
        response = client.post(
            "/api/v1/analyses/test_interrupt_analysis_001/interrupt",
            json=payload
        )
        
        self.assertEqual(response.status_code, 200)
        res_data = response.json()
        
        # Assert returned state contains human context, updated query, and shows completion
        self.assertEqual(res_data.get("status"), "completed")
        self.assertFalse(res_data.get("awaiting_human"))
        self.assertEqual(res_data.get("human_context"), "Issue started after deployment 245")
        self.assertEqual(res_data.get("resume_count"), 1)
        self.assertIsNotNone(res_data.get("last_interrupted_at"))
        
        rag_query = res_data.get("rag_query", "")
        print(f"Final rag_query: '{rag_query}'")
        self.assertIn("deployment 245", rag_query.lower())
        
        # Ensure findings and incident_events were correctly populated
        findings = res_data.get("findings", [])
        events = res_data.get("incident_events", [])
        
        # Log Query Agent finding + RAG Agent finding + correlation + report = 4
        self.assertEqual(len(findings), 4)
        self.assertEqual(findings[0]["agent"], "log_query_agent")
        self.assertEqual(findings[1]["agent"], "rag_agent")
        self.assertEqual(findings[1]["type"], "runbook")
        self.assertEqual(findings[1]["runbook_id"], "RB-100")
        self.assertEqual(findings[2]["agent"], "correlation_agent")
        self.assertEqual(findings[3]["agent"], "report_agent")
        
        # Incident events count = 4
        self.assertEqual(len(events), 4)
        self.assertEqual(events[0]["source"], "log_query_agent")
        self.assertEqual(events[1]["source"], "rag_agent")
        self.assertEqual(events[1]["event_type"], "runbook_match")
        
        print("✅ Phase 2 passed successfully! Graph resumed, resolved, and completed.")

        print("\n--- Test Phase 3: Validate 409 Conflict endpoint check ---")
        # Triggering interrupt again on completed analysis should raise 409 Conflict
        conflict_response = client.post(
            "/api/v1/analyses/test_interrupt_analysis_001/interrupt",
            json=payload
        )
        self.assertEqual(conflict_response.status_code, 409)
        conflict_data = conflict_response.json()
        self.assertEqual(conflict_data["error"]["code"], "ANALYSIS_NOT_AWAITING_HUMAN")
        print("✅ Phase 3 passed successfully! Conflict validation functions correctly.")

    def test_resume_limit_reached(self):
        print("\n--- Test Phase 4: Resume limit check (fails on 3rd resume) ---")
        initial_state = {
            "analysis_id": "test_limit_analysis_002",
            "incident_title": "Payment API Latency Spike",
            "incident_summary": "P95 latency exceeded threshold"
        }
        
        result = run_analysis(initial_state)
        self.assertEqual(result.get("status"), "awaiting_human")
        self.assertEqual(result.get("resume_count"), 0)
        
        # 1st resume -> resume_count = 1
        payload = {
            "interrupt_type": "provide_context",
            "payload": {
                "message": "First operator context"
            },
            "provided_by": "operator"
        }
        res1 = client.post("/api/v1/analyses/test_limit_analysis_002/interrupt", json=payload)
        self.assertEqual(res1.status_code, 200)
        self.assertEqual(res1.json().get("status"), "awaiting_human")
        self.assertEqual(res1.json().get("resume_count"), 1)
        
        # 2nd resume -> resume_count = 2
        payload["payload"]["message"] = "Second operator context"
        res2 = client.post("/api/v1/analyses/test_limit_analysis_002/interrupt", json=payload)
        self.assertEqual(res2.status_code, 200)
        self.assertEqual(res2.json().get("status"), "awaiting_human")
        self.assertEqual(res2.json().get("resume_count"), 2)
        
        # 3rd resume -> resume_count = 3 (exceeds limit of 2)
        payload["payload"]["message"] = "Third operator context"
        res3 = client.post("/api/v1/analyses/test_limit_analysis_002/interrupt", json=payload)
        self.assertEqual(res3.status_code, 200)
        self.assertEqual(res3.json().get("status"), "failed")
        self.assertFalse(res3.json().get("awaiting_human"))
        self.assertEqual(res3.json().get("resume_count"), 3)
        print("✅ Phase 4 passed successfully! Analysis marked failed when exceeding limit of 2 resumptions.")

if __name__ == "__main__":
    unittest.main()
