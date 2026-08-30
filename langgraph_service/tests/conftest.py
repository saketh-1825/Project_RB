import fakeredis

import internal.redis_client

fake_redis = fakeredis.FakeRedis(decode_responses=True)

# Override the global _redis_conn directly
internal.redis_client._redis_conn = fake_redis

from unittest.mock import MagicMock, patch

# And override the module-level imports
import pytest

import internal.websocket_manager


@pytest.fixture(autouse=True, scope="session")
def mock_graph_backend_client():
    """
    Patches the GoBackendClient used by notify_transition()
    in workflow/graph.py so node transition notifications
    never make real HTTP calls during tests.
    """
    with patch("workflow.graph.GoBackendClient") as mock_cls:
        mock_instance = MagicMock()
        mock_cls.return_value = mock_instance
        mock_instance.patch_incident.return_value = {"status": "ok"}
        yield mock_instance
