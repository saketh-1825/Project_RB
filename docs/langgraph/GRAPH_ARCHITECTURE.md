# LangGraph Architecture

## Why LangGraph?
LangGraph was selected because our SRE AI copilot requires complex, stateful workflows that simple agents cannot handle reliably. LangGraph provides:
- **Stateful Execution:** Allows passing a structured `AnalysisState` across multiple steps.
- **Cycles and Branches:** Supports advanced routing (e.g., Confidence Router).
- **Human-in-the-Loop:** Enables pausing the graph, awaiting human review, and resuming exactly where it left off.
- **Persistence:** Integrates smoothly with Redis for saving graph checkpoints and state.

## Node Responsibilities
Each node in the graph has a specific role:
1. **Supervisor Agent:** Entry point. Parses the initial alert, creates an incident in the Go backend, and initializes the analysis state.
2. **Analysis Coordinator:** Detects overlapping or concurrent alerts to link related analyses and prevent duplicated effort.
3. **Evidence Agent:** Gathers context (logs, metrics, runbooks, etc.) from various backend services.
4. **Correlation Agent:** Analyzes evidence to determine root causes and calculates a confidence score.
5. **Confidence Router:** A routing function (not an LLM node) that checks the confidence score and decides the next step (Report Agent or Human Review).
6. **Human Review Node:** Pauses execution if confidence is low, waiting for an SRE to provide context.
7. **Report Agent:** Generates the final post-mortem/incident report and marks the analysis as completed.

## Routing Logic
The graph uses conditional edges to route execution dynamically:
- After `evidence_agent`, the flow routes to `correlation_agent` (unless already awaiting human review).
- After `correlation_agent`, the `Confidence Router` evaluates the calculated confidence score:
  - If score >= 0.75 or backend is degraded: Routes to `Report Agent`.
  - If score < 0.75: Routes to `Human Review`.

## Human Interrupt Flow
When routed to `Human Review`, the graph saves a checkpoint to Redis and enters an `awaiting_human` state. The SRE copilot API exposes endpoints for an engineer to provide context. Once provided, the graph is resumed, picking up the new context to re-evaluate (usually routing back to the `Correlation Agent`).

## ASCII Graph

```text
                  START
                    |
                    v

              Supervisor
                    |
                    v

          Analysis Coordinator

                    |
                    v

            Evidence Agent

                    |
                    v

          Correlation Agent

                    |
                    v

          Confidence Router

              HIGH       LOW

               |          |

               v          v

          Report Agent   Human Review

               |          |

               |       Resume

               |          |

               |          v

               |    Correlation Agent

               |

               v

              END
```
