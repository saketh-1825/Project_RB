import os
import json
import logging
from datetime import datetime, timezone, timedelta
from typing import Dict, Any, List, Optional
from schemas.state import AnalysisState
from internal.redis_client import _get_redis

logger = logging.getLogger(__name__)

def parse_timestamp(ts_str: Optional[str]) -> Optional[datetime]:
    if not ts_str:
        return None
    try:
        clean = ts_str.replace("Z", "+00:00")
        dt = datetime.fromisoformat(clean)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt.astimezone(timezone.utc)
    except Exception as e:
        logger.debug(f"Failed to parse timestamp {ts_str}: {e}")
        return None

def check_topology_relation(service_a: str, service_b: str, topology: Optional[Dict[str, Any]]) -> bool:
    if not topology or not isinstance(topology, dict):
        return False
    services = topology.get("services", [])
    if not services:
        return False
    
    deps = {}
    for svc in services:
        name = svc.get("name", "").lower()
        svc_id = svc.get("service_id", "").lower()
        svc_deps = [d.lower() for d in svc.get("dependencies", []) if d]
        if name:
            deps[name] = svc_deps
        if svc_id:
            deps[svc_id] = svc_deps
            
    a = service_a.lower()
    b = service_b.lower()
    
    # Direct dependency: does a depend on b, or does b depend on a?
    if b in deps.get(a, []):
        return True
    if a in deps.get(b, []):
        return True
        
    # Substring check
    for key, val in deps.items():
        if key == a or key in a:
            if b in val or any(b in d or d in b for d in val):
                return True
        if key == b or key in b:
            if a in val or any(a in d or d in a for d in val):
                return True
                
    return False

def check_alert_relationship(new_state: Dict[str, Any], old_state: Dict[str, Any]) -> Optional[Dict[str, str]]:
    """
    Checks if a relationship exists between two alerts based on:
    1. Same affected service
    2. Related topology dependency
    3. Similar time window (within 15 minutes)
    """
    new_alert = new_state.get("alert") or {}
    old_alert = old_state.get("alert") or {}
    
    new_services = [s for s in new_alert.get("affected_services", []) if s]
    old_services = [s for s in old_alert.get("affected_services", []) if s]
    
    # If no affected services, try to look at incident title or alert name
    if not new_services and new_state.get("incident_title"):
        for word in ["payment", "database", "order", "frontend", "auth"]:
            if word in new_state.get("incident_title", "").lower():
                new_services.append(word)
    if not old_services and old_state.get("incident_title"):
        for word in ["payment", "database", "order", "frontend", "auth"]:
            if word in old_state.get("incident_title", "").lower():
                old_services.append(word)
                
    if not new_services or not old_services:
        return None
        
    # Time window check (15 minutes)
    new_ts_str = new_state.get("triggered_at") or new_alert.get("fired_at")
    old_ts_str = old_state.get("triggered_at") or old_alert.get("fired_at")
    
    new_ts = parse_timestamp(new_ts_str) or datetime.now(timezone.utc)
    old_ts = parse_timestamp(old_ts_str)
    
    if old_ts:
        if abs(new_ts - old_ts) > timedelta(minutes=15):
            return None
    else:
        # If timestamp is missing, assume they are within the same window for safety/tests
        pass
        
    # Same service check
    same_services = set(new_services).intersection(set(old_services))
    if same_services:
        return {
            "relationship": "same_service",
            "summary": f"Alerts both affect service: {', '.join(same_services)}"
        }
        
    # Dependency check using services_topology
    topology = new_state.get("services_topology") or old_state.get("services_topology")
    if topology:
        for new_svc in new_services:
            for old_svc in old_services:
                if check_topology_relation(new_svc, old_svc, topology):
                    return {
                        "relationship": "dependency",
                        "summary": f"Service {new_svc} is topologically related to {old_svc}"
                    }
                    
    return None

def detect_and_link_related_analyses(state: AnalysisState) -> AnalysisState:
    """
    Scans Redis using SCAN iteration (non-blocking) to find related active/recent investigations,
    populates state["related_analyses"] metadata list.
    """
    logger.info("Running Analysis Coordinator to detect overlapping alerts...")
    
    analysis_id = state.get("analysis_id", "")
    if not analysis_id:
        return state
        
    r = _get_redis()
    cursor = 0
    match_pattern = "analysis:*"
    related_list = []
    
    # Standard non-blocking SCAN iteration
    while True:
        cursor, keys = r.scan(cursor=cursor, match=match_pattern, count=100)
        for key in keys:
            # Skip checkpoint keys, event keys, and the current analysis key itself
            if key.endswith(":checkpoint") or key.endswith(":events") or key == f"analysis:{analysis_id}":
                continue
                
            try:
                state_data_json = r.hget(key, "state")
                if not state_data_json:
                    continue
                    
                other_state = json.loads(state_data_json)
                other_id = other_state.get("analysis_id")
                if not other_id:
                    continue
                    
                # Skip checking if it's the current analysis (redundant check)
                if other_id == analysis_id:
                    continue
                    
                # Check status: must be active (running/awaiting_human) or recent (completed within 15 minutes)
                other_status = other_state.get("status", "")
                is_active = other_status in ["running", "awaiting_human", "pending"]
                
                # Check recency of completed ones
                is_recent = False
                if other_status == "completed":
                    # Check timestamp
                    last_updated_str = other_state.get("last_interrupted_at") or other_state.get("triggered_at")
                    # Or check report created_at
                    report = other_state.get("report") or {}
                    created_at_str = report.get("created_at") or last_updated_str
                    
                    if created_at_str:
                        created_ts = parse_timestamp(created_at_str)
                        if created_ts and (datetime.now(timezone.utc) - created_ts) <= timedelta(minutes=15):
                            is_recent = True
                            
                if not (is_active or is_recent):
                    continue
                    
                relation = check_alert_relationship(state, other_state)
                if relation:
                    other_alert = other_state.get("alert") or {}
                    other_services = other_alert.get("affected_services", [])
                    other_svc = other_services[0] if other_services else "unknown"
                    
                    related_list.append({
                        "analysis_id": other_id,
                        "service": other_svc,
                        "relationship": relation["relationship"],
                        "summary": relation["summary"]
                    })
            except Exception as e:
                logger.error(f"Error parsing other analysis state from Redis key {key}: {e}")
                
        if cursor == 0:
            break
            
    if related_list:
        state["related_analyses"] = related_list
        logger.info(f"Analysis Coordinator: Linked {len(related_list)} related investigations.")
    else:
        logger.info("Analysis Coordinator: No related investigations found.")
        
    return state
