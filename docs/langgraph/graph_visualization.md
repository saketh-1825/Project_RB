# LangGraph Visualization

This document provides visual representations of our SRE Copilot LangGraph architecture.

## ASCII Diagram

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

## Mermaid Diagram

```mermaid
graph TD
    START --> Supervisor
    Supervisor --> AnalysisCoordinator
    AnalysisCoordinator --> EvidenceAgent
    EvidenceAgent --> CorrelationAgent
    CorrelationAgent --> ConfidenceRouter
    ConfidenceRouter -->|HIGH| ReportAgent
    ConfidenceRouter -->|LOW| HumanReview
    HumanReview --> Resume
    Resume --> CorrelationAgent
    ReportAgent --> END
```
