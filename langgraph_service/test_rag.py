import sys
import os
import json

# Ensure python can find local imports
sys.path.append(os.path.dirname(os.path.abspath(__file__)))

from workflow.graph import get_graph

def run_test():
    print("🤖 Starting LangGraph workflow integration test...")
    graph = get_graph()
    
    initial_state = {
        "incident_title": "Payment API Latency Spike",
        "incident_summary": "P95 latency exceeded threshold"
    }
    
    print(f"Invoking graph with input:\n{json.dumps(initial_state, indent=2)}")
    
    result = graph.invoke(initial_state)
    
    print(f"Graph execution complete! Final state keys: {list(result.keys())}")
    
    findings = result.get("findings", [])
    incident_events = result.get("incident_events", [])
    rag_query = result.get("rag_query")
    
    print("\n--- Output Findings ---")
    print(json.dumps(findings, indent=2))
    
    print("\n--- Output Incident Events ---")
    print(json.dumps(incident_events, indent=2))
    
    print(f"\nPrepared RAG query: '{rag_query}'")
    print(f"Findings count: {len(findings)}")
    print(f"Incident events count: {len(incident_events)}")
    
    # Assertions based on test requirements:
    # 1. findings count = 2
    # 2. findings[0]["agent"] = "log_query_agent"
    # 3. findings[1]["agent"] = "rag_agent"
    # 4. incident_events length >= 2
    
    errors = []
    if len(findings) != 2:
        errors.append(f"Expected exactly 2 findings, got {len(findings)}")
    else:
        if findings[0].get("agent") != "log_query_agent":
            errors.append(f"Expected findings[0] to be from 'log_query_agent', got '{findings[0].get('agent')}'")
        if findings[1].get("agent") != "rag_agent":
            errors.append(f"Expected findings[1] to be from 'rag_agent', got '{findings[1].get('agent')}'")
            
    if len(incident_events) < 2:
        errors.append(f"Expected at least 2 incident events, got {len(incident_events)}")
        
    if errors:
        print("\n❌ Test FAILED with following errors:")
        for err in errors:
            print(f"- {err}")
        sys.exit(1)
        
    print("\n✅ Test PASSED successfully! All constraints and expectations satisfied.")
    sys.exit(0)

if __name__ == "__main__":
    run_test()
