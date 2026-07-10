# Checkpoint and Resume Mechanism

LangGraph supports saving and loading graph state using a checkpointer. In our architecture, we persist state to **Redis** to ensure resilience, observability, and the ability to pause for human intervention.

## Redis Storage Structure

We store three types of keys for each analysis:

1. **`analysis:{id}`**
   - Contains the latest complete JSON state payload.
   - Updated at the end of every complete run or resume cycle.
   
2. **`analysis:{id}:checkpoint`**
   - The LangGraph-native checkpointer format.
   - Used by LangGraph internally to restore graph memory threads.
   
3. **`analysis:{id}:events`**
   - A Redis List containing append-only JSON event objects (e.g., `analysis.started`, `analysis.agent_switched`, `analysis.finding`).
   - Serves as an audit log and drives WebSocket streams for the UI.

## Human-in-the-Loop Flow (Day 17)

When the Correlation Agent calculates a `LOW` confidence score, the Confidence Router directs the graph to the **Human Review Node**.

1. **Pause**: The Human Review Node sets `awaiting_human = True` and records `waiting_at = 'confidence_review'`. The node then finishes, and LangGraph hits the `END` node, saving the state to Redis.
2. **Review**: The SRE receives an alert, views the evidence, and submits context via the backend API.
3. **Resume**: The API updates the state with `human_context` and calls the `resume_analysis()` function.
4. **Re-evaluate**: `resume_analysis` uses the `waiting_at` property to dynamically update the internal checkpointer and resume execution from the previous node (`Correlation Agent`), allowing it to incorporate the human context.

## Failure Recovery

If a node crashes unexpectedly:
- An `analysis.failed` event is emitted.
- The state is saved with `status = "failed"`.
- The Redis checkpoint remains intact. 
Because state changes are persisted up to the failing node, we avoid losing previously gathered evidence.
