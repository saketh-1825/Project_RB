from typing import TypedDict, List, Optional, Dict, Any

class AnalysisState(TypedDict, total=False):
    analysis_id: str
    alert: dict
    incident_id: Optional[str]
    findings: List[Dict[str, Any]]
    current_agent: str
    status: str
    report: Optional[dict]
    
    # Topology and correlation fields
    services_topology: Optional[dict]
    correlation: Optional[dict]
    time_window: Optional[Dict[str, str]]
    metrics_data: Optional[Dict[str, Any]]
    metrics_summary: Optional[Dict[str, Any]]
    similar_incidents: Optional[List[Dict[str, Any]]]
    root_cause: Optional[Dict[str, Any]]
    correlation_finding: Optional[Dict[str, Any]]
    evidence: Optional[dict]
    backend_health: Optional[str]

    # RAG/Incident specific fields
    incident_title: str
    incident_summary: str
    incident_events: List[Dict[str, Any]]
    rag_query: str

    # Human-in-the-loop interrupt fields
    human_context: Optional[str]
    awaiting_human: bool
    waiting_at: Optional[str]
    interrupt_type: Optional[str]
    interrupt_question: Optional[str]
    resume_count: int
    last_interrupted_at: Optional[str]