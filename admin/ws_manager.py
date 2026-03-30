"""
WebSocket connection manager for PLC4X Manager.

Manages real-time push to browser clients via WebSocket rooms.
MQTT→WebSocket bridge is started in main.py lifespan.
"""

from __future__ import annotations

import asyncio
import json
import logging
from typing import Optional

from fastapi import WebSocket

log = logging.getLogger(__name__)

_MAX_CONNECTIONS = 200
_PING_TIMEOUT = 5.0       # seconds to wait for pong
_SEND_TIMEOUT = 5.0       # seconds per individual client send
_REAPER_INTERVAL = 60     # seconds between dead-connection sweeps


class ConnectionManager:
    """Manages WebSocket connections organized by room.

    Thread-safety model:
    - asyncio.Lock guards mutations to the connections dict.
    - broadcast() collects a snapshot of sockets under the lock,
      then fans out with asyncio.gather (concurrent, not sequential).
    """

    def __init__(self) -> None:
        # room → set of WebSocket objects
        self._connections: dict[str, set[WebSocket]] = {}
        self._lock = asyncio.Lock()
        self._reaper_task: Optional[asyncio.Task] = None

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    def start_reaper(self) -> None:
        """Start the dead-connection reaper background task.

        Must be called from within a running event loop (e.g. lifespan startup).
        """
        if self._reaper_task is None or self._reaper_task.done():
            self._reaper_task = asyncio.create_task(self._reaper_loop())

    async def stop_reaper(self) -> None:
        """Cancel the reaper task gracefully."""
        if self._reaper_task and not self._reaper_task.done():
            self._reaper_task.cancel()
            try:
                await self._reaper_task
            except asyncio.CancelledError:
                pass

    # ------------------------------------------------------------------
    # Connection management
    # ------------------------------------------------------------------

    async def connect(self, ws: WebSocket, room: str = "live") -> bool:
        """Accept the WebSocket and add it to *room*.

        Returns False if the connection limit has been reached.
        The caller is responsible for calling ws.accept() BEFORE auth checks;
        this method does NOT call ws.accept() again.
        """
        async with self._lock:
            total = sum(len(s) for s in self._connections.values())
            if total >= _MAX_CONNECTIONS:
                return False
            self._connections.setdefault(room, set()).add(ws)
            return True

    # Keep add_connection as an alias matching main.py spec
    async def add_connection(self, ws: WebSocket, room: str = "live") -> bool:
        return await self.connect(ws, room)

    async def disconnect(self, ws: WebSocket, room: str = "live") -> None:
        """Remove *ws* from *room*."""
        async with self._lock:
            sockets = self._connections.get(room)
            if sockets:
                sockets.discard(ws)
                if not sockets:
                    del self._connections[room]

    # Keep remove_connection as an alias matching main.py spec
    async def remove_connection(self, ws: WebSocket, room: str = "live") -> None:
        return await self.disconnect(ws, room)

    # ------------------------------------------------------------------
    # Broadcasting
    # ------------------------------------------------------------------

    async def broadcast(self, data: dict | str, room: str = "live") -> None:
        """Send *data* to all clients in *room* concurrently.

        Each send has an individual _SEND_TIMEOUT second timeout. Failed
        sends (timeout or disconnect) silently remove the offending socket.
        """
        # Encode once
        if isinstance(data, dict):
            text = json.dumps(data)
        else:
            text = str(data)

        # Snapshot under the lock — do NOT hold lock during I/O
        async with self._lock:
            sockets = set(self._connections.get(room, set()))

        if not sockets:
            return

        async def _send(ws: WebSocket) -> Optional[WebSocket]:
            """Try to send; return the socket on failure so it can be reaped."""
            try:
                await asyncio.wait_for(ws.send_text(text), timeout=_SEND_TIMEOUT)
                return None
            except Exception:
                return ws

        results = await asyncio.gather(*(_send(ws) for ws in sockets))
        failed = [ws for ws in results if ws is not None]

        if failed:
            async with self._lock:
                room_set = self._connections.get(room, set())
                for ws in failed:
                    room_set.discard(ws)
                if not room_set and room in self._connections:
                    del self._connections[room]

    # ------------------------------------------------------------------
    # Properties
    # ------------------------------------------------------------------

    @property
    def client_count(self) -> int:
        """Total connected WebSocket clients across all rooms."""
        return sum(len(s) for s in self._connections.values())

    # ------------------------------------------------------------------
    # Dead-connection reaper
    # ------------------------------------------------------------------

    async def _reaper_loop(self) -> None:
        """Periodically ping every connection; remove those that do not respond."""
        while True:
            try:
                await asyncio.sleep(_REAPER_INTERVAL)
                await self._reap_dead_connections()
            except asyncio.CancelledError:
                break
            except Exception as exc:
                log.warning("WS reaper error: %s", exc)

    async def _reap_dead_connections(self) -> None:
        # Snapshot all (room, ws) pairs
        async with self._lock:
            pairs: list[tuple[str, WebSocket]] = [
                (room, ws)
                for room, sockets in self._connections.items()
                for ws in set(sockets)
            ]

        dead: list[tuple[str, WebSocket]] = []

        async def _ping(room: str, ws: WebSocket) -> Optional[tuple[str, WebSocket]]:
            try:
                await asyncio.wait_for(ws.send_text('{"type":"ping"}'), timeout=_PING_TIMEOUT)
                return None
            except Exception:
                return (room, ws)

        results = await asyncio.gather(*(_ping(room, ws) for room, ws in pairs))

        for item in results:
            if item is not None:
                dead.append(item)

        if dead:
            async with self._lock:
                for room, ws in dead:
                    room_set = self._connections.get(room, set())
                    room_set.discard(ws)
                    if not room_set and room in self._connections:
                        del self._connections[room]
            log.info("WS reaper removed %d dead connections", len(dead))


# Module-level singleton
manager = ConnectionManager()
