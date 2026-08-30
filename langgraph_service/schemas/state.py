from typing import Any, TypedDict


class AnalysisState(TypedDict, total=False):
    analysis_id: str
    alert: dict[str, Any]
    incident_id: str | None
    findings: list[dict[str, Any]]
    current_agent: str
    status: str
    report: dict[str, Any] | None
    report_status: str | None

    # Topology and correlation fields
    services_topology: dict[str, Any] | None
    correlation: dict[str, Any] | None
    time_window: dict[str, str] | None
    metrics_data: dict[str, Any] | None
    metrics_summary: dict[str, Any] | None
    similar_incidents: list[dict[str, Any]] | None
    root_cause: dict[str, Any] | None
    correlation_finding: dict[str, Any] | None
    evidence: dict[str, Any] | None
    evidence_quality: dict[str, Any] | None
    risk_assessment: dict[str, Any] | None
    backend_health: str | None

    # RAG/Incident specific fields
    incident_title: str
    incident_summary: str
    incident_events: list[dict[str, Any]]
    rag_query: str | None

    # Human-in-the-loop interrupt fields
    human_context: str | None
    awaiting_human: bool
    waiting_at: str | None
    interrupt_type: str | None
    interrupt_question: str | None
    resume_count: int
    last_interrupted_at: str | None

    # Dedicated Human Review Node fields
    review_reason: str | None
    requires_input: bool | None
