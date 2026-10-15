"""WebSocket endpoint: /ws/brain.

Streams live events from the JARVIS event bus to connected clients.

On connect:
  - Client receives a `hello` message with server metadata.
  - Client is added to the event subscriber list; every event emitted
    by the running system is forwarded as JSON.

On disconnect:
  - Client is removed from the subscriber list.

Design notes:
  - The EventLogger stores subscribers at class level. Our callback
    here is an async function; the broadcaster runs a background task
    per connection that drains a queue. This keeps the synchronous
    emit() path from blocking on WebSocket send.
  - If a client stalls and the queue fills, we drop the oldest events
    rather than block or crash.
"""

import asyncio
import json
from contextlib import suppress

from fastapi import APIRouter, WebSocket, WebSocketDisconnect

from ..db import EventLogger


router = APIRouter()


# Max events buffered per connection. Prevents unbounded memory use
# if a client stalls and stops reading.
_MAX_QUEUE = 200


class _Connection:
    """Per-connection state: a bounded queue and a subscriber callback.

    The event bus emits from arbitrary threads (Hermes scheduler,
    lifecycle hooks, etc). asyncio.Queue is NOT thread-safe across
    event loops, so we capture the connection's loop and bridge
    every enqueue via loop.call_soon_threadsafe().
    """

    def __init__(self, loop: asyncio.AbstractEventLoop):
        self.loop = loop
        self.queue: asyncio.Queue = asyncio.Queue(maxsize=_MAX_QUEUE)

    def _enqueue(self, payload: dict) -> None:
        """Runs ON the connection's event loop."""
        try:
            self.queue.put_nowait(payload)
        except asyncio.QueueFull:
            with suppress(asyncio.QueueEmpty):
                self.queue.get_nowait()  # drop oldest
            with suppress(asyncio.QueueFull):
                self.queue.put_nowait(payload)

    def on_event(self, payload: dict) -> None:
        """Called by EventLogger.emit() from any thread."""
        self.loop.call_soon_threadsafe(self._enqueue, payload)

async def _sender(websocket: WebSocket, conn: _Connection) -> None:
    """Drain the queue and send events to the client.

    Runs until cancelled. Any exception (including WebSocketDisconnect)
    terminates the loop. The caller handles cleanup.
    """
    while True:
        payload = await conn.queue.get()
        await websocket.send_text(json.dumps(payload))
 
@router.websocket("/ws/brain")
async def brain_websocket(websocket: WebSocket) -> None:
    await websocket.accept()

    conn = _Connection(asyncio.get_running_loop())

    # Send the hello message. If this fails, the client never truly
    # connected — bail out immediately.
    try:
        await websocket.send_text(
            json.dumps(
                {
                    "type": "hello",
                    "service": "JARVIS",
                    "version": "0.1.0",
                    "phase": "8b",
                }
            )
        )
    except Exception:
        try:
            await websocket.close()
        except Exception:
            pass
        return

    # Subscribe to the event bus BEFORE we start sending.
    EventLogger.subscribe(conn.on_event)

    sender_task = asyncio.create_task(_sender(websocket, conn))

    try:
        # Wait until the client disconnects or the sender dies. We use
        # asyncio.wait() so neither task can leak — if EITHER completes,
        # we tear down cleanly.
        receive_task = asyncio.create_task(websocket.receive_text())
        done, pending = await asyncio.wait(
            {receive_task, sender_task},
            return_when=asyncio.FIRST_COMPLETED,
        )
        # Cancel whatever is still running
        for task in pending:
            task.cancel()
            with suppress(asyncio.CancelledError, Exception):
                await task
        # Drain any exception from completed tasks so nothing leaks
        for task in done:
            with suppress(asyncio.CancelledError, Exception):
                task.result()
    finally:
        EventLogger.unsubscribe(conn.on_event)
        # Ensure sender is truly stopped
        if not sender_task.done():
            sender_task.cancel()
            with suppress(asyncio.CancelledError, Exception):
                await sender_task
