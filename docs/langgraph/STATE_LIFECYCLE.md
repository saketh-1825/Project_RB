# AnalysisState Lifecycle

The `AnalysisState` is a `TypedDict` that acts as the single source of truth passed between LangGraph nodes. Since nodes are stateless functions, they communicate entirely by mutating and returning this shared state object.

## State Evolution

### 1. Initial State (Input to graph)
```json
{
  "analysis_id": "uuid-1234",
  "alert": {
    "name": "High CPU",
    "affected_services": ["api-gateway"]
  }
}
```

### 2. After Supervisor Agent
The Supervisor processes the alert and opens an incident in the Go backend.
```json
{
  "analysis_id": "uuid-1234",
  "alert": {...},
  "incident_id": "inc-5678",
  "status": "running",
  "current_agent": "supervisor"
}
```

### 3. After Evidence Agent
The Evidence Agent executes sub-collectors concurrently (logs, metrics, RAG, topology).
```json
{
  "analysis_id": "uuid-1234",
  "incident_id": "inc-5678",
  "services_topology": {"nodes": [...]},
  "evidence": {
    "logs": {"findings": [...]},
    "metrics": {"root_cause": ...},
    "rag": {"findings": [...]},
    "topology": {...}
  },
  "findings": [...],
  "incident_events": [...]
}
```

### 4. After Correlation Agent
The Correlation Agent synthesizes the evidence to find the root cause and calculates a confidence score.
```json
{
  "correlation": {
    "confidence": {
      "score": 0.90,
      "level": "HIGH"
    }
  },
  "root_cause": {
    "type": "Code deployment issue"
  }
}
```

### 5. After Report Agent (Final)
If confidence was high, or human review completed, the Report Agent summarizes findings.
```json
{
  "status": "completed",
  "report": {
    "title": "Post-Mortem: High CPU in api-gateway",
    "summary": "...",
    "action_items": [...]
  }
}
```

## Why Shared State?
LangGraph requires a single state object to handle branching and cyclic logic seamlessly. This avoids global variables, making the graph highly concurrent, deterministic, and easily serializable for checkpoints.
