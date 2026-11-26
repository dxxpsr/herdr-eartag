"""Herdr socket API client.

Protocol: send one line of JSON (plus newline) as a request over a Unix socket, and read one line back.
After `events.subscribe`, events keep arriving one line at a time on the same connection.
"""
from __future__ import annotations

import itertools
import json
import select
import socket
from typing import List, Optional

# Events the daemon uses as a "something changed" signal. Their contents are not interpreted; the snapshot is re-read.
SUBSCRIBED_EVENTS: List[str] = [
    "pane.updated",  # includes terminal title changes (= Claude session name)
    "pane.focused",
    "pane.created",
    "pane.closed",
    "pane.exited",
    "pane.agent_detected",
    "tab.created",
    "tab.focused",
    "tab.renamed",  # detects when the user renames a tab
    "layout.updated",
]

_request_ids = itertools.count(1)


class HerdrError(Exception):
    """Failed to connect to Herdr, or Herdr returned an error."""


def _next_id() -> str:
    return f"eartag-{next(_request_ids)}"


def _connect(socket_path: str, timeout: float) -> socket.socket:
    sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    sock.settimeout(timeout)
    try:
        sock.connect(socket_path)
    except OSError as exc:
        sock.close()
        # Fold FileNotFoundError, ConnectionRefusedError, etc. into one exception to keep callers simple.
        raise HerdrError(f"Cannot connect to the Herdr socket: {socket_path} ({exc})") from exc
    return sock


def _encode(method: str, params: dict) -> bytes:
    payload = {"id": _next_id(), "method": method, "params": params}
    return (json.dumps(payload, ensure_ascii=False) + "\n").encode("utf-8")


def _parse_response(line: str, method: str) -> dict:
    if not line:
        raise HerdrError(f"Herdr closed the connection without replying: {method}")
    try:
        message = json.loads(line)
    except ValueError as exc:
        raise HerdrError(f"Cannot parse the Herdr reply: {method}") from exc
    if "error" in message:
        err = message["error"] or {}
        raise HerdrError(f"Herdr error ({method}): {err.get('code')} {err.get('message')}")
    result = message.get("result")
    if not isinstance(result, dict):
        raise HerdrError(f"Herdr reply has no result: {method}")
    return result


def request(socket_path: str, method: str, params: dict, timeout: float = 3.0) -> dict:
    """Send one request and return the reply's result."""
    sock = _connect(socket_path, timeout)
    try:
        sock.sendall(_encode(method, params))
        line = sock.makefile("r", encoding="utf-8").readline()
    except OSError as exc:
        raise HerdrError(f"Communication error during Herdr request: {method} ({exc})") from exc
    finally:
        sock.close()
    return _parse_response(line, method)


def _field(result: dict, key: str, method: str) -> dict:
    """Pull the expected object out of a reply's result. Reports an unexpected shape as HerdrError instead of KeyError."""
    value = result.get(key)
    if not isinstance(value, dict):
        raise HerdrError(f"Unexpected Herdr reply format: {method} (missing result.{key})")
    return value


def get_snapshot(socket_path: str) -> dict:
    """Fetch the full state of the current session (tabs, layouts, panes)."""
    return _field(request(socket_path, "session.snapshot", {}), "snapshot", "session.snapshot")


def rename_tab(socket_path: str, tab_id: str, label: str) -> None:
    request(socket_path, "tab.rename", {"tab_id": tab_id, "label": label})


def get_tab(socket_path: str, tab_id: str) -> dict:
    """Current info for one tab (used to re-check its label)."""
    return _field(request(socket_path, "tab.get", {"tab_id": tab_id}), "tab", "tab.get")


def get_process_info(socket_path: str, pane_id: str) -> dict:
    """Foreground process info for a pane. Observed: foreground_processes can be missing (e.g. top)."""
    return _field(request(socket_path, "pane.process_info", {"pane_id": pane_id}), "process_info", "pane.process_info")


class EventStream:
    """An events.subscribe connection. wait() only reports whether an event arrived."""

    def __init__(self, socket_path: str, subscribe_timeout: float = 3.0) -> None:
        # The subscription reply comes right away, so read it with a short timeout and use select afterwards.
        self._sock = _connect(socket_path, timeout=subscribe_timeout)
        self._buffer = b""
        try:
            subscriptions = [{"type": name} for name in SUBSCRIBED_EVENTS]
            self._sock.sendall(_encode("events.subscribe", {"subscriptions": subscriptions}))
            first = self._read_line(subscribe_timeout)
            if first is None:
                raise HerdrError("Timed out waiting for the Herdr subscription reply")
            result = _parse_response(first.decode("utf-8"), "events.subscribe")
            if result.get("type") != "subscription_started":
                raise HerdrError(f"Unexpected subscription reply: {result.get('type')}")
        except (HerdrError, OSError) as exc:
            self.close()
            if isinstance(exc, HerdrError):
                raise
            raise HerdrError(f"Herdr event subscription failed: {exc}") from exc

    def _read_line(self, timeout: float) -> Optional[bytes]:
        """Read one line. None on timeout, b'' if the connection closed."""
        while b"\n" not in self._buffer:
            ready, _, _ = select.select([self._sock], [], [], timeout)
            if not ready:
                return None
            chunk = self._sock.recv(65536)
            if not chunk:
                return b""
            self._buffer += chunk
        line, self._buffer = self._buffer.split(b"\n", 1)
        return line

    def wait(self, timeout: float) -> str:
        """Consume one event. Result: "event" | "timeout" | "closed"."""
        try:
            line = self._read_line(timeout)
        except OSError:
            return "closed"
        if line is None:
            return "timeout"
        if line == b"":
            return "closed"
        return "event"

    def close(self) -> None:
        try:
            self._sock.close()
        except OSError:
            pass
