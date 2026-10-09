"""Phase 8b: tests for the /ws/brain WebSocket endpoint.

Verifies:
- The route exists and accepts connections
- A `hello` message is sent on connect
- Events emitted via EventLogger.emit() are forwarded to clients
- Disconnection cleanly removes the subscriber

Uses FastAPI's TestClient.websocket_connect(), which runs the ASGI
app in-process — no live server required.
"""

import json
import time
from threading import Thread

from fastapi.testclient import TestClient

from jarvis.app.main import app
from jarvis.app.db import EventLogger
from jarvis.core.events import Event


client = TestClient(app)


def test_websocket_route_exists():
    """The /ws/brain route must be registered on the app."""
    paths = [
        r.path for r in app.routes
        if hasattr(r, "path")
    ]
    assert "/ws/brain" in paths


def test_websocket_accepts_connection_and_sends_hello():
    """On connect, the server sends a hello message with service metadata."""
    with client.websocket_connect("/ws/brain") as ws:
        data = ws.receive_text()
        payload = json.loads(data)
        assert payload["type"] == "hello"
        assert payload["service"] == "JARVIS"
        assert payload["version"] == "0.1.0"
        assert payload["phase"] == "8b"


def test_websocket_forwards_emitted_events():
    """Events emitted via EventLogger.emit() reach connected clients."""
    with client.websocket_connect("/ws/brain") as ws:
        # Consume the hello.
        hello = json.loads(ws.receive_text())
        assert hello["type"] == "hello"

        # Emit an event from a background thread (mimics Hermes emitting
        # from a scheduler thread while the WebSocket is open).
        def emit_soon():
            time.sleep(0.05)
            EventLogger().emit(
                Event(
                    kind="TEST_EVENT",
                    message="hello from test",
                    source="TEST",
                    component="TEST",
                    severity="INFO",
                )
            )

        Thread(target=emit_soon, daemon=True).start()

        # Receive the forwarded event.
        data = ws.receive_text()
        payload = json.loads(data)
        assert payload["kind"] == "TEST_EVENT"
        assert payload["message"] == "hello from test"
        assert payload["source"] == "TEST"


def test_websocket_unsubscribe_on_disconnect():
    """After disconnect, the subscriber list should be cleaned up."""
    before = len(EventLogger._subscribers)

    with client.websocket_connect("/ws/brain") as ws:
        ws.receive_text()  # hello
        during = len(EventLogger._subscribers)
        assert during == before + 1

    # After context exit, subscriber should be removed.
    # Give the finally block a moment to run.
    for _ in range(20):
        if len(EventLogger._subscribers) == before:
            break
        time.sleep(0.05)

    after = len(EventLogger._subscribers)
    assert after == before, f"Subscriber count {after} != {before} after disconnect"


def test_multiple_clients_receive_same_event():
    """Two concurrent connections should both receive the same event."""
    with client.websocket_connect("/ws/brain") as ws1:
        ws1.receive_text()  # hello

        with client.websocket_connect("/ws/brain") as ws2:
            ws2.receive_text()  # hello

            def emit_soon():
                time.sleep(0.05)
                EventLogger().emit(
                    Event(
                        kind="BROADCAST_TEST",
                        message="both should see this",
                        source="TEST",
                    )
                )

            Thread(target=emit_soon, daemon=True).start()

            d1 = json.loads(ws1.receive_text())
            d2 = json.loads(ws2.receive_text())

            assert d1["kind"] == "BROADCAST_TEST"
            assert d2["kind"] == "BROADCAST_TEST"
            assert d1["event_id"] == d2["event_id"]
