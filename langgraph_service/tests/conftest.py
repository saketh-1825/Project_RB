import pytest
import fakeredis
import internal.redis_client
import internal.analysis_coordinator
import sys

fake_redis = fakeredis.FakeRedis(decode_responses=True)

# Override the global _redis_conn directly
internal.redis_client._redis_conn = fake_redis

import internal.websocket_manager
internal.websocket_manager.redis_conn = fake_redis

# And override the module-level imports
internal.analysis_coordinator._get_redis = lambda: fake_redis
