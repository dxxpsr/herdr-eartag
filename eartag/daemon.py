"""Event subscription daemon.

Behavior: when an event arrives (a signal that something changed), re-read the snapshot and fix tab names.
Why event contents are not interpreted: re-reading a snapshot of a few tabs is simpler than working out
which event affects which tab, and it never misses a case.
"""
from __future__ import annotations

import copy
import fcntl
import hashlib
import logging
import os
import socket
import time
from contextlib import contextmanager
from logging.handlers import RotatingFileHandler
from typing import IO, Dict, Iterator, Optional, Tuple

from .git_branch import current_branch
from .herdr_client import EventStream, HerdrError, get_process_info, get_snapshot, get_tab, rename_tab
from .naming import needs_process_info, plan_tabs
from .state import (
    adopt_pinned,
    begin_session,
    load_state,
    pin_tab,
    prune_state,
    refresh_pinned,
    remember_label,
    save_state,
    unpin_tab,
)

logger = logging.getLogger("herdr_eartag")

# Recompute this often even without events (covers a cd the shell does not report in its title, and missed events).
IDLE_RECHECK_SECONDS = 5.0
# Window for folding a burst of events (focus + layout + title, etc.) into one recompute.
DEBOUNCE_SECONDS = 0.15
# How long, and how often, to retry when the connection drops (server restart, handoff).
RECONNECT_SECONDS = 10.0
RECONNECT_INTERVAL = 0.5
# During a handoff the previous daemon may still hold the lock, so wait a moment.
LOCK_RETRY_SECONDS = 5.0

LOG_FILE_NAME = "eartag.log"
# State file name used by v1 (not split per session). Adopted once on upgrade.
LEGACY_STATE_FILE_NAME = "labels.json"


def _socket_digest(socket_path: str) -> str:
    """Short hash used to split files per Herdr session (socket), so multiple `--session` servers do not mix."""
    return hashlib.sha1(socket_path.encode("utf-8")).hexdigest()[:8]


def state_file(state_dir: str, socket_path: str) -> str:
    return os.path.join(state_dir, f"labels-{_socket_digest(socket_path)}.json")


def state_lock_path(state_dir: str, socket_path: str) -> str:
    return os.path.join(state_dir, f"state-{_socket_digest(socket_path)}.lock")


def lock_path(state_dir: str, socket_path: str) -> str:
    """Lock file that keeps a second daemon from running."""
    return os.path.join(state_dir, f"daemon-{_socket_digest(socket_path)}.lock")


def log_file(state_dir: str) -> str:
    return os.path.join(state_dir, LOG_FILE_NAME)


def setup_logging(state_dir: str) -> None:
    """Log to the state dir. Rotate past 1MB so the log does not keep growing on disk."""
    os.makedirs(state_dir, exist_ok=True)
    handler = RotatingFileHandler(log_file(state_dir), maxBytes=1_000_000, backupCount=1, encoding="utf-8")
    handler.setFormatter(logging.Formatter("%(asctime)s [%(levelname)s] %(message)s"))
    root = logging.getLogger()
    root.handlers[:] = [handler]
    root.setLevel(logging.INFO)


@contextmanager
def _state_lock(state_dir: str, socket_path: str) -> Iterator[None]:
    """Guards the state file from read to write, so the resync action and the daemon never interleave records."""
    path = state_lock_path(state_dir, socket_path)
    os.makedirs(state_dir, exist_ok=True)
    with open(path, "a+", encoding="utf-8") as handle:
        # A recompute takes a few ms, so waiting on a blocking lock is fine.
        fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
        try:
            yield
        finally:
            fcntl.flock(handle.fileno(), fcntl.LOCK_UN)


def _local_host() -> str:
    return socket.gethostname().split(".")[0]


def _collect_process_infos(socket_path: str, snapshot: dict) -> Dict[str, dict]:
    """Query process info only for focused panes without an agent. Panes that fail are left out (handled by the folder name rule)."""
    panes_by_id = {p.get("pane_id"): p for p in snapshot.get("panes", [])}
    infos: Dict[str, dict] = {}
    for layout in snapshot.get("layouts", []):
        pane_id = layout.get("focused_pane_id")
        pane = panes_by_id.get(pane_id)
        if not pane_id or pane is None or not needs_process_info(pane):
            continue
        try:
            infos[pane_id] = get_process_info(socket_path, pane_id)
        except HerdrError as exc:
            logger.debug("Process info lookup failed (falling back to folder name): %s (%s)", pane_id, exc)
    return infos


def _collect_branches(snapshot: dict) -> Dict[str, str]:
    """git branch per focused pane without an agent (the only labels that show it). Panes outside a repo are left out."""
    panes_by_id = {p.get("pane_id"): p for p in snapshot.get("panes", [])}
    branches: Dict[str, str] = {}
    for layout in snapshot.get("layouts", []):
        pane_id = layout.get("focused_pane_id")
        pane = panes_by_id.get(pane_id)
        if not pane_id or pane is None or not needs_process_info(pane):
            continue
        cwd = pane.get("foreground_cwd") or pane.get("cwd")
        branch = current_branch(cwd) if isinstance(cwd, str) else None
        if branch:
            branches[pane_id] = branch
    return branches


def _adopt_legacy_state(state_dir: str, path: str) -> None:
    """Adopt the v1 state file (labels.json, not split per session) into the per-session file.

    Without this, every name the plugin had set would freeze as a "user-set name" right after the upgrade.
    Call only while holding the state lock.
    """
    legacy = os.path.join(state_dir, LEGACY_STATE_FILE_NAME)
    if os.path.exists(path) or not os.path.exists(legacy):
        return
    try:
        save_state(path, load_state(legacy))  # load_state converts the v1 format
        os.unlink(legacy)
        logger.info("Adopted the previous state file: %s", LEGACY_STATE_FILE_NAME)
    except OSError as exc:
        logger.warning("Could not adopt the previous state file (continuing with empty state): %s", exc)


def server_id(socket_path: str) -> Optional[str]:
    """Identifies a Herdr server instance. A new server creates a new socket file, so its inode and birth time change.

    After a restart, tab IDs may be reassigned, so tab records must be cleared (state.begin_session).
    If it cannot be determined, returns None and tab records are kept.
    """
    try:
        st = os.stat(socket_path)
    except OSError:
        return None
    created = getattr(st, "st_birthtime", st.st_ctime)
    return f"{st.st_ino}:{int(created)}"


def reconcile_once(socket_path: str, state_dir: str) -> int:
    """Fix tab names once from a snapshot. Returns how many tabs were actually renamed."""
    with _state_lock(state_dir, socket_path):
        snapshot = get_snapshot(socket_path)
        tabs = snapshot.get("tabs", [])
        path = state_file(state_dir, socket_path)
        _adopt_legacy_state(state_dir, path)
        state = load_state(path)
        stored = copy.deepcopy(state)
        begin_session(state, server_id(socket_path))
        prune_state(
            state,
            {t.get("workspace_id") for t in tabs if t.get("workspace_id")},
            {t.get("tab_id") for t in tabs if t.get("tab_id")},
        )
        # Pins first: after a restart a pinned tab must be recognized before planning, or it gets renamed.
        adopt_pinned(state, tabs)
        refresh_pinned(state, tabs)

        process_infos = _collect_process_infos(socket_path, snapshot)
        plans = plan_tabs(
            snapshot,
            state,
            process_infos,
            home=os.path.expanduser("~"),
            local_host=_local_host(),
            branches=_collect_branches(snapshot),
        )

        changed = 0
        try:
            for tab_id, workspace_id, current, desired in plans:
                # Refresh the record even for unchanged tabs, so a long-unchanged tab is not pushed out of the record and frozen.
                remember_label(state, workspace_id, tab_id, current)
                if desired is None or desired == current:
                    continue
                try:
                    # The user may have renamed the tab after the snapshot was read, so check again right before renaming.
                    latest = get_tab(socket_path, tab_id).get("label")
                    if latest != current:
                        logger.info("Tab renamed in the meantime, skipping: %s '%s'", tab_id, latest)
                        continue
                    # Record before renaming: if the reply times out after the rename applied, the new label is still on record and does not freeze.
                    remember_label(state, workspace_id, tab_id, desired)
                    rename_tab(socket_path, tab_id, desired)
                except HerdrError as exc:
                    # One failing tab (closed in the meantime, etc.) does not stop the others.
                    logger.warning("Tab rename failed: %s → '%s' (%s)", tab_id, desired, exc)
                    continue
                logger.info("Tab renamed: %s '%s' → '%s'", tab_id, current, desired)
                changed += 1
        finally:
            # Keep the records made so far even if an exception or termination signal arrives midway.
            # Skip the write when nothing changed (avoids a needless disk write every 5 seconds).
            if state != stored:
                save_state(path, state)
        return changed


def _tab_workspace(tab: dict, tab_id: str) -> str:
    """Workspace of a tab. tab.get reports it; the ID prefix ("w1:t2" → "w1") is the fallback."""
    workspace_id = tab.get("workspace_id")
    if isinstance(workspace_id, str) and workspace_id:
        return workspace_id
    return tab_id.split(":", 1)[0]


def pin_tab_now(socket_path: str, state_dir: str, tab_id: str) -> str:
    """Freeze a tab's current name. Returns that name. Raises HerdrError if the tab cannot be read."""
    with _state_lock(state_dir, socket_path):
        tab = get_tab(socket_path, tab_id)
        label = tab.get("label")
        if not isinstance(label, str):
            raise HerdrError(f"Tab has no label: {tab_id}")
        path = state_file(state_dir, socket_path)
        state = load_state(path)
        begin_session(state, server_id(socket_path))
        pin_tab(state, _tab_workspace(tab, tab_id), tab_id, label)
        save_state(path, state)
    logger.info("Tab pinned: %s '%s'", tab_id, label)
    return label


def release_tab_now(socket_path: str, state_dir: str, tab_id: str) -> Tuple[bool, int]:
    """Hand a tab (pinned or user-named) back to automatic naming and rename it right away.

    Returns (was pinned, number of tabs renamed). Recording the current label as one the plugin set is what
    makes a user-named tab managed again (is_managed trusts the tab's own record).
    """
    with _state_lock(state_dir, socket_path):
        tab = get_tab(socket_path, tab_id)
        label = tab.get("label")
        if not isinstance(label, str):
            raise HerdrError(f"Tab has no label: {tab_id}")
        workspace_id = _tab_workspace(tab, tab_id)
        path = state_file(state_dir, socket_path)
        state = load_state(path)
        begin_session(state, server_id(socket_path))
        was_pinned = unpin_tab(state, workspace_id, tab_id)
        remember_label(state, workspace_id, tab_id, label)
        save_state(path, state)
    logger.info("Tab released: %s '%s' (was pinned: %s)", tab_id, label, was_pinned)
    # reconcile_once takes the state lock itself, so it must run after the block above has released it.
    return was_pinned, reconcile_once(socket_path, state_dir)


def try_acquire_lock(path: str) -> Optional[IO[str]]:
    """Try a non-blocking lock. None if another daemon already holds it."""
    os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
    handle = open(path, "a+", encoding="utf-8")
    try:
        fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        handle.close()
        return None
    # Write the pid only after taking the lock (used by the status and stop commands).
    handle.seek(0)
    handle.truncate()
    handle.write(f"{os.getpid()}\n")
    handle.flush()
    return handle


def acquire_lock_with_retry(path: str, seconds: float = LOCK_RETRY_SECONDS, interval: float = 0.25) -> Optional[IO[str]]:
    """Retry the lock for `seconds`, to wait for a previous daemon that is about to exit during a handoff."""
    deadline = time.monotonic() + seconds
    while True:
        handle = try_acquire_lock(path)
        if handle is not None or time.monotonic() >= deadline:
            return handle
        time.sleep(interval)


def connect_stream(
    socket_path: str, retry_seconds: float = RECONNECT_SECONDS, interval: float = RECONNECT_INTERVAL
) -> Optional[EventStream]:
    """Open the subscription. The server may be briefly absent, so retry for retry_seconds."""
    deadline = time.monotonic() + retry_seconds
    while True:
        try:
            return EventStream(socket_path)
        except HerdrError as exc:
            if time.monotonic() >= deadline:
                logger.error("Could not reconnect to Herdr, exiting: %s", exc)
                return None
        time.sleep(interval)


def _safe_reconcile(socket_path: str, state_dir: str) -> None:
    try:
        reconcile_once(socket_path, state_dir)
    except HerdrError as exc:
        logger.warning("Recompute failed (will retry on the next event): %s", exc)
    except Exception:  # noqa: BLE001 — log only, so an unexpected error does not kill the daemon.
        logger.exception("Unexpected error during recompute")


def _drain_burst(stream: EventStream) -> str:
    """Consume every event in a burst for DEBOUNCE_SECONDS. Result: "ok" | "closed"."""
    deadline = time.monotonic() + DEBOUNCE_SECONDS
    while True:
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            return "ok"
        result = stream.wait(remaining)
        if result == "closed":
            return "closed"
        if result == "timeout":
            return "ok"


def run_daemon(socket_path: str, state_dir: str) -> int:
    """The subscription loop. Reconnects when the connection drops and exits if that fails (the next server's startup relaunches it)."""
    lock = acquire_lock_with_retry(lock_path(state_dir, socket_path))
    if lock is None:
        return 0  # Another daemon is running. Exit before logging is set up so the log file is untouched.
    try:
        setup_logging(state_dir)
        stream = connect_stream(socket_path)
        if stream is None:
            return 0
        logger.info("Daemon started (pid=%s)", os.getpid())
        _safe_reconcile(socket_path, state_dir)
        while True:
            result = stream.wait(IDLE_RECHECK_SECONDS)  # "event" | "timeout" | "closed"
            if result == "event":
                result = _drain_burst(stream)  # "ok" | "closed"
            if result == "closed":
                stream.close()
                logger.info("Herdr connection lost, reconnecting")
                stream = connect_stream(socket_path)
                if stream is None:
                    return 0
                logger.info("Reconnected to Herdr")
            _safe_reconcile(socket_path, state_dir)
    finally:
        lock.close()
