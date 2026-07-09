import sys
import os
import unittest

sys.path.append(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from agents.correlation_agent import analyze_evidence_quality, calculate_risk

class TestEvidenceQuality(unittest.TestCase):
    def setUp(self):
        self.base_evidence = {
            "metadata": {
                "collection_status": {
                    "logs": "success",
                    "metrics": "success",
                    "rag": "success",
                    "topology": "success"
                }
            },
            "logs": {
                "findings": [
                    {
                        "type": "log_anomaly",
                        "degraded": False,
                        "evidence": {"log_ids": ["123"]}
                    }
                ]
            },
            "metrics": {
                "metrics_query_failed": False,
                "metrics_response": {
                    "series": [
                        {
                            "metric_name": "error_rate",
                            "data_points": [
                                {"timestamp": "2026-03-29T10:00:00Z", "value": 0.0},
                                {"timestamp": "2026-03-29T10:01:00Z", "value": 0.0},
                                {"timestamp": "2026-03-29T10:02:00Z", "value": 0.0},
                                {"timestamp": "2026-03-29T10:03:00Z", "value": 0.8}, # Spike
                                {"timestamp": "2026-03-29T10:04:00Z", "value": 0.9}
                            ]
                        }
                    ]
                }
            },
            "rag": {
                "findings": [
                    {"type": "runbook", "similarity_score": 0.9}
                ]
            },
            "topology": {
                "services": [{"name": "payment-api"}]
            }
        }
        self.base_state = {}

    def test_complete_evidence(self):
        result = analyze_evidence_quality(self.base_evidence, self.base_state)
        
        # Complete evidence should yield available sources and score of 1.0
        self.assertIn("logs", result["available_sources"])
        self.assertIn("metrics", result["available_sources"])
        self.assertIn("rag", result["available_sources"])
        self.assertIn("topology", result["available_sources"])
        self.assertEqual(len(result["missing_sources"]), 0)
        self.assertEqual(len(result["conflicts"]), 0)
        self.assertAlmostEqual(result["quality_score"], 1.0)

    def test_missing_metrics(self):
        # Remove metrics completely
        self.base_evidence["metadata"]["collection_status"]["metrics"] = "failed"
        self.base_evidence["metrics"] = {"metrics_query_failed": True}
        
        result = analyze_evidence_quality(self.base_evidence, self.base_state)
        
        # Available sources shouldn't include metrics
        self.assertNotIn("metrics", result["available_sources"])
        # Missing sources should track metrics
        missing_sources = [m["source"] for m in result["missing_sources"]]
        self.assertIn("metrics", missing_sources)
        # Quality score should be less
        self.assertAlmostEqual(result["quality_score"], 0.75)

    def test_conflicting_evidence(self):
        # Set logs to show errors but metrics to have no anomalies (no spikes)
        self.base_evidence["metrics"]["metrics_response"]["series"][0]["data_points"] = [{"timestamp": "2026-03-29T10:00:00Z", "value": 0.0}, {"timestamp": "2026-03-29T10:01:00Z", "value": 0.0}]
        
        result = analyze_evidence_quality(self.base_evidence, self.base_state)
        
        # Conflict should be detected
        self.assertEqual(len(result["conflicts"]), 1)
        self.assertEqual(result["conflicts"][0]["type"], "LOG_METRIC_MISMATCH")
        # Quality score should be penalized (1.0 - 0.2 = 0.8)
        self.assertAlmostEqual(result["quality_score"], 0.8)

    def test_risk_scoring(self):
        # Test CRITICAL risk (payment service)
        risk = calculate_risk(
            evidence=self.base_evidence,
            root_cause={"type": "DB_TIMEOUT", "confidence": 0.8},
            affected_services=["payment-api"],
            alert={"severity": "medium"}
        )
        self.assertEqual(risk["level"], "CRITICAL")
        
        # Test HIGH risk (confirmed root cause, not payment, low error rate)
        high_risk_evidence = dict(self.base_evidence)
        high_risk_evidence["metrics"] = {
            "metrics_query_failed": False,
            "metrics_response": {
                "series": [
                    {
                        "metric_name": "error_rate",
                        "data_points": [{"timestamp": "2026-03-29T10:00:00Z", "value": 0.0}, {"timestamp": "2026-03-29T10:01:00Z", "value": 0.1}]
                    }
                ]
            }
        }
        risk = calculate_risk(
            evidence=high_risk_evidence,
            root_cause={"type": "CPU_PRESSURE", "confidence": 0.9},
            affected_services=["image-processing"],
            alert={"severity": "medium"}
        )
        self.assertEqual(risk["level"], "HIGH")
        
        # Test MEDIUM risk (partial evidence/missing metrics)
        missing_metrics_ev = dict(self.base_evidence)
        missing_metrics_ev["metrics"] = {"metrics_query_failed": True}
        risk = calculate_risk(
            evidence=missing_metrics_ev,
            root_cause={"type": "UNKNOWN", "confidence": 0.2},
            affected_services=["background-worker"],
            alert={"severity": "medium"}
        )
        self.assertEqual(risk["level"], "MEDIUM")
        
        # Test LOW risk (weak signals)
        weak_ev = {"metadata": {"collection_status": {"metrics": "success"}}}
        risk = calculate_risk(
            evidence=weak_ev,
            root_cause={"type": "UNKNOWN", "confidence": 0.2},
            affected_services=["background-worker"],
            alert={"severity": "low"}
        )
        self.assertEqual(risk["level"], "LOW")

if __name__ == "__main__":
    unittest.main()
