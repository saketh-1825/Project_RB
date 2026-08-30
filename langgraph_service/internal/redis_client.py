import json
import os
from typing import Any

import redis

# Fetch Redis connection URL from environment or default to local container redis
REDIS_URL = os.environ.get("REDIS_URL", "redis://localhost:6379")

# Create a connection pool and client instance
_pool = redis.ConnectionPool.from_url(REDIS_URL, decode_responses=True)
_redis_conn = redis.Redis(connection_pool=_pool)


def _get_redis() -> redis.Redis:
    return _redis_conn


def save_analysis_state(analysis_id: str, state: dict[str, Any]) -> None:
    """
    Saves the full AnalysisState to Redis under the key 'analysis:{analysis_id}'.
    Stores key fields as hash fields along with the full serialized state.
    """
    r = _get_redis()
    key = f"analysis:{analysis_id}"

    # Extract string values or serialize them
    status = str(state.get("status") or "")
    current_agent = str(state.get("current_agent") or "")
    awaiting_human = str(state.get("awaiting_human", False))
    waiting_at = str(state.get("waiting_at") or "")
    interrupt_type = str(state.get("interrupt_type") or "")
    interrupt_question = str(state.get("interrupt_question") or "")
    human_context = str(state.get("human_context") or "")
    full_state_serialized = json.dumps(state)

    mapping = {
        "status": status,
        "current_agent": current_agent,
        "awaiting_human": awaiting_human,
        "waiting_at": waiting_at,
        "interrupt_type": interrupt_type,
        "interrupt_question": interrupt_question,
        "human_context": human_context,
        "state": full_state_serialized,
    }

    r.hset(key, mapping=mapping)
    # Persist the isolated checkpoint JSON explicitly
    r.set(f"analysis:{analysis_id}:checkpoint", full_state_serialized)


def get_analysis_state(analysis_id: str) -> dict[str, Any] | None:
    """
    Retrieves and deserializes the full AnalysisState dict from Redis.
    """
    r = _get_redis()
    key = f"analysis:{analysis_id}"

    if not r.exists(key):
        return None

    full_state_serialized = r.hget(key, "state")
    if not full_state_serialized:
        return None

    try:
        return json.loads(full_state_serialized)
    except (json.JSONDecodeError, TypeError):
        return None


def update_analysis_state(analysis_id: str, state: dict[str, Any]) -> None:
    """
    Updates the AnalysisState in Redis. Since we store the full state,
    updating is identical to saving/overwriting the state.
    """
    save_analysis_state(analysis_id, state)
