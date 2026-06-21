from typing import TypedDict, List, Optional, Dict, Any

class AnalysisState(TypedDict, total=False):
    analysis_id: str
    alert: dict
    incident_id: Optional[str]
    findings: List[Dict[str, Any]]
    current_agent: str
    status: str
    report: Optional[dict]
    
    # RAG/Incident specific fields
    incident_title: str
    incident_summary: str
    incident_events: List[Dict[str, Any]]
    rag_query: str