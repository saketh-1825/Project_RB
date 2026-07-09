import json
import asyncio
import logging
from typing import Dict, Set, Any, Optional
from fastapi import WebSocket

logger = logging.getLogger(__name__)

class WebSocketManager:
    """
    Manages real-time WebSocket connections subscribed to specific analysis streams.
    Saves and replays event histories for frontend timeline synchronization.
    """
    def __init__(self):
        self.active_connections: Dict[str, Set[WebSocket]] = {}
        self._loop: Optional[asyncio.AbstractEventLoop] = None

    def set_loop(self, loop: asyncio.AbstractEventLoop) -> None:
        """Sets reference to the main thread's asyncio event loop."""
        self._loop = loop

    async def connect(self, websocket: WebSocket, analysis_id: str) -> None:
        """Accepts WebSocket connection and replays event history from Redis."""
        # Lazily initialize the loop if not set
        if not self._loop:
            try:
                self._loop = asyncio.get_running_loop()
            except RuntimeError:
                pass

        await websocket.accept()
        if analysis_id not in self.active_connections:
            self.active_connections[analysis_id] = set()
        self.active_connections[analysis_id].add(websocket)
        logger.info(f"WebSocket client connected to analysis stream: {analysis_id}")

        # Replay past events from Redis list (source of truth)
        try:
            from internal.redis_client import _get_redis
            r = _get_redis()
            events_json = r.lrange(f"analysis:{analysis_id}:events", 0, -1)
            # Reverse list since it is stored via LPUSH (newest first) to replay chronologically
            events = [json.loads(ev) for ev in reversed(events_json)]
            for event in events:
                await websocket.send_json(event)
        except Exception as e:
            logger.error(f"Failed to replay event history for analysis {analysis_id}: {e}")

    def disconnect(self, websocket: WebSocket, analysis_id: str) -> None:
        """Unregisters a disconnected WebSocket client."""
        if analysis_id in self.active_connections:
            self.active_connections[analysis_id].discard(websocket)
            if not self.active_connections[analysis_id]:
                del self.active_connections[analysis_id]
        logger.info(f"WebSocket client disconnected from analysis stream: {analysis_id}")

    async def broadcast_event(self, analysis_id: str, event: Dict[str, Any]) -> None:
        """Asynchronously sends an event to all WebSocket clients watching this analysis."""
        connections = self.active_connections.get(analysis_id, set())
        if not connections:
            return

        disconnected: Set[WebSocket] = set()
        for ws in connections:
            try:
                await ws.send_json(event)
            except Exception as e:
                logger.debug(f"Failed to send websocket message: {e}")
                disconnected.add(ws)

        if disconnected:
            for ws in disconnected:
                self.disconnect(ws, analysis_id)

    def broadcast_event_sync(self, analysis_id: str, event: Dict[str, Any]) -> None:
        """
        Thread-safe wrapper to broadcast an event from sync execution threads
        back to the main event loop thread without blocking.
        """
        loop = self._loop
        if not loop:
            try:
                loop = asyncio.get_running_loop()
            except RuntimeError:
                pass

        if loop and loop.is_running():
            asyncio.run_coroutine_threadsafe(self.broadcast_event(analysis_id, event), loop)
        else:
            logger.warning(f"No running event loop. Event not broadcasted: {event.get('event_type')}")

manager = WebSocketManager()
