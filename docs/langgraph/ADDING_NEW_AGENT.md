# Adding a New Agent to the Graph

Our LangGraph architecture is designed to be highly extensible. Follow this guide to safely add a new agent, for example, a `Remediation Agent`.

## 1. Create the Agent Node

Create a new file in `langgraph_service/agents/`:
`agents/remediation_agent.py`

```python
from schemas.state import AnalysisState


def remediation_agent_node(state: AnalysisState) -> AnalysisState:
    root_cause = state.get("root_cause")
    # Execute remediation logic here
    state["remediation_status"] = "success"
    return state
```

## 2. Update the State Schema

Extend the typed dictionary in `schemas/state.py` to support new state keys:

```python
class AnalysisState(TypedDict, total=False):
    # Existing fields...
    remediation_status: Optional[str]
```

## 3. Register the Node

In `workflow/graph.py`, import your node and register it using the `wrap_node` helper (which ensures standard events are emitted):

```python
from agents.remediation_agent import remediation_agent_node

# Track it for event emissions
TRACKED_NODES.append("remediation_agent")

builder.add_node(
    "remediation_agent", wrap_node("remediation_agent", "remediation_agent_node")
)
```

## 4. Add Graph Edges

Determine where your node fits in the workflow. For example, replacing the path from Report Agent to END:

```python
# Instead of builder.add_edge("report_agent", END)
builder.add_edge("report_agent", "remediation_agent")
builder.add_edge("remediation_agent", END)
```

## 5. Add Tests

Add unit and integration tests for your new agent to ensure it behaves correctly when receiving mocked API responses. Be sure to mock any `GoBackendClient` instances used by your agent.
