from typing import TypedDict, List, Optional

class AnalysisState(TypedDict):
    analysis_id: str
    alert: dict
    incident_id: Optional[str]
    findings: List[dict]
    current_agent: str
    status: str
    report: Optional[dict]