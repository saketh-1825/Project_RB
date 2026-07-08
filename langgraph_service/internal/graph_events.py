import json
import logging
from datetime import datetime, timezone
from typing import Dict, Any

logger = logging.getLogger(__name__)

def emit_event(
    analysis_id: str,
    event_type: str,
    node: str,
    status: str,
    payload: Dict[str, Any]
) -> None:
    """
    Constructs, persists, and broadcasts a graph lifecycle event.
    Guarantees that event emission failures will never disrupt graph execution.
    """
    timestamp = datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")
    
    event = {
        "analysis_id": analysis_id,
        "event_type": event_type,
        "node": node,
        "status": status,
        "timestamp": timestamp,
        "payload": payload
    }
    
    # 1. Persist to Redis event history (list under key analysis:{analysis_id}:events)
    try:
        from internal.redis_client import _get_redis
        r = _get_redis()
        list_key = f"analysis:{analysis_id}:events"
        
        # Store serialized JSON string
        event_str = json.dumps(event)
        r.lpush(list_key, event_str)
        r.ltrim(list_key, 0, 99)  # Keep only the latest 100 events
    except Exception as e:
        logger.error(f"Event system failure: Failed to persist event to Redis: {e}")

    # 2. Broadcast event to registered WebSocket connections
    try:
        from internal.websocket_manager import manager
        manager.broadcast_event_sync(analysis_id, event)
    except Exception as e:
        logger.error(f"Event system failure: Failed to broadcast event via WebSocket: {e}")
