"""Tests for the Herdr socket client, using a temporary AF_UNIX fake server on a thread."""
from __future__ import annotations

import json
import os
import socket
import tempfile
import threading
import unittest
from typing import Callable, List

from eartag.herdr_client import EventStream, HerdrError, get_snapshot, get_tab, request


class FakeServer:
    """Fake server that accepts one connection, reads one request line and runs handler(request dict, connection)."""

    def __init__(self, handler: Callable[[dict, socket.socket], None]) -> None:
        # Use a short temp folder to stay under the macOS Unix socket path limit (104 characters).
        self._dir = tempfile.mkdtemp(prefix="han-", dir="/tmp")
        self.path = os.path.join(self._dir, "s.sock")
        self.requests: List[dict] = []
        self._sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        self._sock.bind(self.path)
        self._sock.listen(1)
        self._handler = handler
        self._thread = threading.Thread(target=self._serve, daemon=True)
        self._thread.start()

    def _serve(self) -> None:
        conn, _ = self._sock.accept()
        with conn:
            line = conn.makefile("r", encoding="utf-8").readline()
            req = json.loads(line)
            self.requests.append(req)
            self._handler(req, conn)

    def close(self) -> None:
        self._thread.join(timeout=2)
        self._sock.close()
        os.unlink(self.path)
        os.rmdir(self._dir)


def _send(conn: socket.socket, obj: dict) -> None:
    conn.sendall((json.dumps(obj) + "\n").encode("utf-8"))


class RequestTest(unittest.TestCase):
    def test_request_returns_result(self) -> None:
        server = FakeServer(lambda req, conn: _send(conn, {"id": req["id"], "result": {"type": "x", "v": 1}}))
        try:
            self.assertEqual(request(server.path, "tab.list", {}), {"type": "x", "v": 1})
            self.assertEqual(server.requests[0]["method"], "tab.list")
            self.assertTrue(server.requests[0]["id"].startswith("eartag-"))
        finally:
            server.close()

    def test_request_error_raises(self) -> None:
        server = FakeServer(
            lambda req, conn: _send(conn, {"id": req["id"], "error": {"code": "c", "message": "m"}})
        )
        try:
            with self.assertRaises(HerdrError):
                request(server.path, "tab.rename", {"tab_id": "x", "label": "y"})
        finally:
            server.close()

    def test_unexpected_response_shape_raises_herdr_error(self) -> None:
        # A reply missing the expected key must raise HerdrError, not KeyError (prevents action tracebacks and aborted recomputes).
        for call in (lambda path: get_snapshot(path), lambda path: get_tab(path, "w1:t1")):
            server = FakeServer(lambda req, conn: _send(conn, {"id": req["id"], "result": {"type": "other"}}))
            try:
                with self.assertRaises(HerdrError):
                    call(server.path)
            finally:
                server.close()

    def test_missing_socket_raises_herdr_error(self) -> None:
        with self.assertRaises(HerdrError):
            request("/tmp/does-not-exist-eartag.sock", "ping", {})


class EventStreamTest(unittest.TestCase):
    def test_event_stream_event_then_closed(self) -> None:
        def handler(req: dict, conn: socket.socket) -> None:
            _send(conn, {"id": req["id"], "result": {"type": "subscription_started"}})
            _send(conn, {"event": "pane.updated", "data": {}})
            # Returning closes the connection.

        server = FakeServer(handler)
        try:
            stream = EventStream(server.path)
            self.assertEqual(server.requests[0]["method"], "events.subscribe")
            self.assertEqual(stream.wait(1), "event")
            self.assertEqual(stream.wait(1), "closed")
            stream.close()
        finally:
            server.close()

    def test_event_stream_timeout(self) -> None:
        done = threading.Event()

        def handler(req: dict, conn: socket.socket) -> None:
            _send(conn, {"id": req["id"], "result": {"type": "subscription_started"}})
            done.wait(2)  # keep the connection open without sending anything until the test ends

        server = FakeServer(handler)
        try:
            stream = EventStream(server.path)
            self.assertEqual(stream.wait(0.2), "timeout")
            stream.close()
        finally:
            done.set()
            server.close()

    def test_event_stream_subscribe_timeout_message(self) -> None:
        done = threading.Event()
        server = FakeServer(lambda req, conn: done.wait(2))  # never sends the subscription reply
        try:
            with self.assertRaises(HerdrError) as ctx:
                EventStream(server.path, subscribe_timeout=0.2)
            self.assertIn("Timed out", str(ctx.exception))
        finally:
            done.set()
            server.close()

    def test_event_stream_rejected_subscription_raises(self) -> None:
        server = FakeServer(
            lambda req, conn: _send(conn, {"id": req["id"], "error": {"code": "invalid_request", "message": "m"}})
        )
        try:
            with self.assertRaises(HerdrError):
                EventStream(server.path)
        finally:
            server.close()


class SubscribedEventsTest(unittest.TestCase):
    def test_no_pane_scoped_subscriptions(self) -> None:
        # Observed (Herdr 0.9.5): these types require a pane_id, and one invalid entry makes the whole
        # events.subscribe fail ("missing field `pane_id`"), which stops the daemon.
        from eartag.herdr_client import SUBSCRIBED_EVENTS

        pane_scoped = {"pane.agent_status_changed", "pane.output_matched", "pane.scroll_changed"}
        self.assertFalse(pane_scoped & set(SUBSCRIBED_EVENTS))
        self.assertIn("workspace.updated", SUBSCRIBED_EVENTS)


if __name__ == "__main__":
    unittest.main()
