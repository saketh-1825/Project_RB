from typing import TypedDict, List, Optional


class Finding(TypedDict):

    agent: str

    type: str

    summary: str



class AnalysisState(TypedDict):

    alert_id: str

    status: str

    findings: List[Finding]

    current_agent: Optional[str]

    incident_id: Optional[str]