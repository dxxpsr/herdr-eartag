#!/usr/bin/env python3
"""herdr-eartag entry point.

Subcommands:
  start   Launch the daemon in the background and exit right away (used by [[startup]] and actions)
  daemon  The subscription loop itself (run internally by start)
  resync  Fix tab names once, right now
  status  Whether the daemon is running, plus recent log lines
  stop    Stop the daemon

Uses HERDR_SOCKET_PATH and HERDR_PLUGIN_STATE_DIR, which the Herdr plugin runtime provides.
Environment problems print a message and exit with code 0 so a failed startup does not clutter the Herdr server log.
"""
from __future__ import annotations

import argparse
import fcntl
import os
import signal
import subprocess
import sys
from typing import List, Optional, Tuple

# Put the plugin folder on the import path so the eartag package is found even under -E -s.
PLUGIN_ROOT = os.path.dirname(os.path.abspath(__file__))
if PLUGIN_ROOT not in sys.path:
    sys.path.insert(0, PLUGIN_ROOT)

from eartag import daemon  # noqa: E402
from eartag.herdr_client import HerdrError  # noqa: E402

STATUS_LOG_LINES = 10


def _read_env() -> Optional[Tuple[str, str]]:
    """(socket path, state dir). If either is missing, print a hint and return None."""
    socket_path = os.environ.get("HERDR_SOCKET_PATH")
    state_dir = os.environ.get("HERDR_PLUGIN_STATE_DIR")
    missing = [name for name, value in (("HERDR_SOCKET_PATH", socket_path), ("HERDR_PLUGIN_STATE_DIR", state_dir)) if not value]
    if missing:
        print(
            f"Missing environment variables: {', '.join(missing)} — run this as a Herdr plugin action "
            "(e.g. herdr plugin action invoke herdr-eartag.resync)",
            file=sys.stderr,
        )
        return None
    assert socket_path and state_dir  # filtered by the guard above (for type narrowing)
    return socket_path, state_dir


def cmd_start(socket_path: str, state_dir: str) -> int:
    # No socket means the Herdr server is not running; a daemon would exit immediately, so just say so.
    if not os.path.exists(socket_path):
        print(f"Herdr socket not found, daemon not started: {socket_path}")
        return 0
    # Detach into a new session so the daemon keeps running after the startup command exits.
    # Duplicate launches are blocked by the daemon's own lock, so no check is needed here.
    subprocess.Popen(
        [sys.executable, "-E", "-s", os.path.abspath(__file__), "daemon"],
        start_new_session=True,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        env=os.environ.copy(),
        cwd=PLUGIN_ROOT,
    )
    print("eartag daemon start requested")
    return 0


def cmd_daemon(socket_path: str, state_dir: str) -> int:
    # run_daemon sets up logging only after taking the lock, so a duplicate daemon that is about to exit never touches the log file.
    return daemon.run_daemon(socket_path, state_dir)


def cmd_resync(socket_path: str, state_dir: str) -> int:
    try:
        changed = daemon.reconcile_once(socket_path, state_dir)
    except HerdrError as exc:
        print(f"Could not update tab names: {exc}")
        return 0
    except OSError as exc:
        # State dir permissions, disk problems, etc. Print a message so the action does not end in a traceback.
        print(f"Could not read or write the state file: {exc}")
        return 0
    print(f"Renamed {changed} tab(s)")
    return 0


def _running_pid(lock_file: str) -> Optional[int]:
    """The daemon pid if the lock is held (-1 if the pid cannot be read), otherwise None.

    Decides by lock state rather than trusting the pid file alone (avoids stale pids left by a crash).
    The check takes a shared lock only briefly, so if it overlaps a daemon start, the daemon's lock retry still gets the lock.
    The caller treats OSError as "unknown".
    """
    if not os.path.exists(lock_file):
        return None
    with open(lock_file, "r", encoding="utf-8") as handle:
        try:
            fcntl.flock(handle.fileno(), fcntl.LOCK_SH | fcntl.LOCK_NB)
        except BlockingIOError:
            content = handle.read().strip()
            return int(content) if content.isdigit() else -1
        fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
        return None


def cmd_status(socket_path: str, state_dir: str) -> int:
    try:
        pid = _running_pid(daemon.lock_path(state_dir, socket_path))
        print(f"Daemon: running (pid={pid})" if pid else "Daemon: not running")
    except OSError as exc:
        print(f"Daemon: unknown ({exc})")
    log_path = daemon.log_file(state_dir)
    try:
        with open(log_path, encoding="utf-8", errors="replace") as f:
            lines = f.readlines()[-STATUS_LOG_LINES:]
    except OSError:
        return 0  # no log yet
    print("Recent log:")
    print("".join(lines).rstrip())
    return 0


def cmd_stop(socket_path: str, state_dir: str) -> int:
    try:
        pid = _running_pid(daemon.lock_path(state_dir, socket_path))
    except OSError as exc:
        print(f"Could not check daemon status: {exc}")
        return 0
    if pid is None:
        print("No daemon running")
        return 0
    if pid < 0:
        # The lock is held, so a daemon exists, but its pid could not be read from the lock file.
        print("The daemon is running but its pid could not be read, so it was not stopped. Restarting Herdr will clean it up.")
        return 0
    try:
        os.kill(pid, signal.SIGTERM)
    except ProcessLookupError:
        print("No daemon running")
        return 0
    except OSError as exc:
        print(f"Could not stop the daemon (pid={pid}): {exc}")
        return 0
    print(f"Daemon stopped (pid={pid})")
    return 0


COMMANDS = {
    "start": cmd_start,
    "daemon": cmd_daemon,
    "resync": cmd_resync,
    "status": cmd_status,
    "stop": cmd_stop,
}


def main(argv: Optional[List[str]] = None) -> int:
    parser = argparse.ArgumentParser(description="Automatic Herdr tab names")
    parser.add_argument("command", choices=sorted(COMMANDS))
    args = parser.parse_args(argv)
    env = _read_env()
    if env is None:
        return 0
    socket_path, state_dir = env
    return COMMANDS[args.command](socket_path, state_dir)


if __name__ == "__main__":
    sys.exit(main())
