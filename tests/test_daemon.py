"""Tests for daemon recompute, locking and reconnect. Only socket calls are replaced; everything else runs the real code."""
from __future__ import annotations

import copy
import json
import os
import socket
import tempfile
import threading
import time
import unittest
from contextlib import contextmanager
from typing import Dict, Iterator, List, Optional, Tuple
from unittest import mock

from eartag import daemon
from eartag.herdr_client import HerdrError
from eartag.state import empty_state, load_state, remember_label, save_state, tab_record, workspace_labels

SOCK = "/tmp/fake-herdr.sock"


def _snapshot(tabs: List[Tuple[str, str, int, str]], agent: Optional[str] = "claude") -> dict:
    """Build a minimal snapshot from a list of (tab_id, label, number, title)."""
    snap: dict = {"tabs": [], "layouts": [], "panes": []}
    for i, (tab_id, label, number, title) in enumerate(tabs):
        pane_id = f"p{i}"
        snap["tabs"].append({"tab_id": tab_id, "label": label, "number": number, "workspace_id": tab_id.split(":")[0]})
        snap["layouts"].append({"tab_id": tab_id, "focused_pane_id": pane_id})
        snap["panes"].append({"pane_id": pane_id, "agent": agent, "terminal_title_stripped": title, "foreground_cwd": "/w/proj"})
    return snap


@contextmanager
def _herdr(
    snap: dict,
    tab_labels: Optional[Dict[str, str]] = None,
    process_info=None,
    rename_side_effect=None,
    server: str = "srv",
    branch: Optional[str] = None,
) -> Iterator[mock.MagicMock]:
    """Replace get_snapshot/get_tab/get_process_info/rename_tab. Returns the rename mock."""
    labels = {t["tab_id"]: t["label"] for t in snap["tabs"]}
    labels.update(tab_labels or {})

    def fake_get_tab(sock: str, tab_id: str) -> dict:
        if tab_id not in labels:
            raise HerdrError(f"tab not found: {tab_id}")
        return {"tab_id": tab_id, "label": labels[tab_id], "workspace_id": tab_id.split(":")[0]}

    with mock.patch.object(daemon, "server_id", return_value=server), mock.patch.object(
        daemon, "current_branch", return_value=branch
    ), mock.patch.object(
        daemon, "get_snapshot", return_value=snap
    ), mock.patch.object(
        daemon, "get_tab", side_effect=fake_get_tab
    ), mock.patch.object(daemon, "get_process_info", side_effect=process_info or HerdrError("unavailable")), mock.patch.object(
        daemon, "rename_tab", side_effect=rename_side_effect
    ) as rename:
        yield rename


class ReconcileTest(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.state_dir = self._tmp.name
        self.state_path = daemon.state_file(self.state_dir, SOCK)

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def test_reconcile_renames_and_records_state(self) -> None:
        with _herdr(_snapshot([("w1:t1", "1", 1, "Herdr eartag")])) as rename:
            self.assertEqual(daemon.reconcile_once(SOCK, self.state_dir), 1)
        rename.assert_called_once_with(SOCK, "w1:t1", "Herdr eartag")
        state = load_state(self.state_path)
        self.assertEqual(tab_record(state, "w1", "w1:t1"), ["1", "Herdr eartag"])
        self.assertEqual(workspace_labels(state, "w1"), ["Herdr eartag"])

    def test_reconcile_second_run_is_noop(self) -> None:
        snap = _snapshot([("w1:t1", "1", 1, "Herdr eartag")])
        with _herdr(snap):
            daemon.reconcile_once(SOCK, self.state_dir)
        # The snapshot already reflects the plugin's own rename → it must not rename again (prevents an endless event loop).
        renamed = copy.deepcopy(snap)
        renamed["tabs"][0]["label"] = "Herdr eartag"
        with _herdr(renamed) as rename:
            self.assertEqual(daemon.reconcile_once(SOCK, self.state_dir), 0)
        rename.assert_not_called()

    def test_reconcile_respects_manual_label(self) -> None:
        state = empty_state()
        state["server"] = "srv"
        remember_label(state, "w1", "w1:t1", "old")
        save_state(self.state_path, state)
        with _herdr(_snapshot([("w1:t1", "배포 작업", 1, "Herdr eartag")])) as rename:
            self.assertEqual(daemon.reconcile_once(SOCK, self.state_dir), 0)
        rename.assert_not_called()

    def test_reconcile_continues_after_one_failure(self) -> None:
        def fake_rename(sock: str, tab_id: str, label: str) -> None:
            if tab_id == "w1:t1":
                raise HerdrError("failed")

        snap = _snapshot([("w1:t1", "1", 1, "첫째"), ("w1:t2", "2", 2, "둘째")])
        with _herdr(snap, rename_side_effect=fake_rename) as rename:
            self.assertEqual(daemon.reconcile_once(SOCK, self.state_dir), 1)
        self.assertEqual(rename.call_count, 2)
        state = load_state(self.state_path)
        self.assertEqual(tab_record(state, "w1", "w1:t2"), ["2", "둘째"])
        # A failed tab still has its current label on record, so the next recompute retries it (it does not freeze).
        self.assertIn("1", tab_record(state, "w1", "w1:t1"))

    def test_reconcile_prunes_closed_workspaces(self) -> None:
        state = empty_state()
        remember_label(state, "w9", "w9:t1", "x")
        save_state(self.state_path, state)
        with _herdr(_snapshot([("w1:t1", "1", 1, "제목")])):
            daemon.reconcile_once(SOCK, self.state_dir)
        self.assertNotIn("w9", load_state(self.state_path)["workspaces"])

    def test_reconcile_adopts_legacy_state_file(self) -> None:
        # v1 recorded to labels.json ({tab_id: label}) without splitting per session. Without adopting it,
        # every name the plugin had set would freeze as a "user-set name" right after the upgrade.
        with open(os.path.join(self.state_dir, "labels.json"), "w", encoding="utf-8") as f:
            json.dump({"w1:t1": "옛 이름"}, f, ensure_ascii=False)
        with _herdr(_snapshot([("w1:t1", "옛 이름", 1, "새 이름")])) as rename:
            self.assertEqual(daemon.reconcile_once(SOCK, self.state_dir), 1)
        rename.assert_called_once_with(SOCK, "w1:t1", "새 이름")
        self.assertEqual(workspace_labels(load_state(self.state_path), "w1"), ["옛 이름", "새 이름"])
        # After adopting, the old file is removed (prevents applying it again next session).
        self.assertFalse(os.path.exists(os.path.join(self.state_dir, "labels.json")))

    def test_unchanged_tab_stays_managed_after_many_other_labels(self) -> None:
        # From review: a tab whose name never changes must not be pushed out of the 50-label workspace list and freeze.
        state = empty_state()
        state["server"] = "srv"
        remember_label(state, "w1", "w1:t1", "proj")
        save_state(self.state_path, state)
        idle = _snapshot([("w1:t1", "proj", 1, "proj")], agent=None)
        for i in range(60):  # this tab stays the same while other tabs produce 60 different labels
            state = load_state(self.state_path)
            remember_label(state, "w1", f"w1:t{100 + i}", f"other-{i}")
            save_state(self.state_path, state)
            with _herdr(idle) as rename:
                daemon.reconcile_once(SOCK, self.state_dir)
            rename.assert_not_called()
        # Running vim in this tab now must still rename it automatically.
        vim = {"pane_id": "p0", "foreground_process_group_id": 7, "foreground_processes": [{"pid": 7, "name": "vim", "argv0": "vim"}]}
        with _herdr(idle, process_info=lambda sock, pane_id: vim) as rename:
            daemon.reconcile_once(SOCK, self.state_dir)
        rename.assert_called_once_with(SOCK, "w1:t1", "vim · proj")

    def test_server_change_uses_workspace_labels_for_renumbered_tabs(self) -> None:
        # Before restart: w1:t4 has '옛 제목'. After restart the same tab is renumbered w1:t2.
        state = empty_state()
        state["server"] = "old-server"
        remember_label(state, "w1", "w1:t4", "옛 제목")
        save_state(self.state_path, state)
        with _herdr(_snapshot([("w1:t1", "1", 1, "a"), ("w1:t2", "옛 제목", 2, "새 제목")])) as rename:
            daemon.reconcile_once(SOCK, self.state_dir)
        self.assertIn(mock.call(SOCK, "w1:t2", "새 제목"), rename.call_args_list)

    def test_reconcile_uses_process_info_for_non_agent_pane(self) -> None:
        vim = {"pane_id": "p0", "foreground_process_group_id": 7, "foreground_processes": [{"pid": 7, "name": "vim", "argv0": "vim"}]}
        snap = _snapshot([("w1:t1", "1", 1, "")], agent=None)
        with _herdr(snap, process_info=lambda sock, pane_id: vim) as rename:
            daemon.reconcile_once(SOCK, self.state_dir)
        rename.assert_called_once_with(SOCK, "w1:t1", "vim · proj")

    def test_reconcile_process_info_failure_falls_back(self) -> None:
        snap = _snapshot([("w1:t1", "1", 1, "")], agent=None)
        with _herdr(snap) as rename:  # get_process_info raises HerdrError by default
            daemon.reconcile_once(SOCK, self.state_dir)
        rename.assert_called_once_with(SOCK, "w1:t1", "proj")

    def test_reconcile_skips_when_label_changed_meanwhile(self) -> None:
        snap = _snapshot([("w1:t1", "1", 1, "Herdr eartag")])
        with _herdr(snap, tab_labels={"w1:t1": "사용자 이름"}) as rename:
            self.assertEqual(daemon.reconcile_once(SOCK, self.state_dir), 0)
        rename.assert_not_called()


class PathsAndLocksTest(unittest.TestCase):
    def test_state_file_per_socket(self) -> None:
        self.assertNotEqual(daemon.state_file("/s", "/a.sock"), daemon.state_file("/s", "/b.sock"))
        self.assertNotEqual(daemon.lock_path("/s", "/a.sock"), daemon.lock_path("/s", "/b.sock"))

    def test_lock_prevents_second_instance(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = daemon.lock_path(tmp, SOCK)
            first = daemon.try_acquire_lock(path)
            self.assertIsNotNone(first)
            self.assertIsNone(daemon.try_acquire_lock(path))
            with open(path, encoding="utf-8") as f:
                self.assertEqual(f.read().strip(), str(os.getpid()))
            first.close()

    def test_lock_retry_acquires_after_release(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = daemon.lock_path(tmp, SOCK)
            first = daemon.try_acquire_lock(path)
            threading.Timer(0.3, first.close).start()
            second = daemon.acquire_lock_with_retry(path, seconds=3.0, interval=0.05)
            self.assertIsNotNone(second)
            second.close()


class _SubscribeServer:
    """Fake Herdr that opens its socket after `delay` seconds and answers a subscribe request with subscription_started."""

    def __init__(self, path: str, delay: float) -> None:
        self.path = path
        self._stop = threading.Event()
        self._sock: Optional[socket.socket] = None
        self._thread = threading.Thread(target=self._run, args=(delay,), daemon=True)
        self._thread.start()

    def _run(self, delay: float) -> None:
        time.sleep(delay)
        self._sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        self._sock.bind(self.path)
        self._sock.listen(1)
        conn, _ = self._sock.accept()
        with conn:
            req = json.loads(conn.makefile("r", encoding="utf-8").readline())
            conn.sendall((json.dumps({"id": req["id"], "result": {"type": "subscription_started"}}) + "\n").encode())
            self._stop.wait(3)

    def close(self) -> None:
        self._stop.set()
        self._thread.join(timeout=3)
        if self._sock:
            self._sock.close()


class ConnectStreamTest(unittest.TestCase):
    def test_connect_stream_retries_until_server_appears(self) -> None:
        tmp = tempfile.mkdtemp(prefix="han-", dir="/tmp")
        path = os.path.join(tmp, "s.sock")
        server = _SubscribeServer(path, delay=0.6)
        try:
            stream = daemon.connect_stream(path, retry_seconds=3.0, interval=0.1)
            self.assertIsNotNone(stream)
            stream.close()
        finally:
            server.close()
            if os.path.exists(path):
                os.unlink(path)
            os.rmdir(tmp)

    def test_connect_stream_gives_up(self) -> None:
        started = time.monotonic()
        self.assertIsNone(daemon.connect_stream("/tmp/no-such-eartag.sock", retry_seconds=0.6, interval=0.1))
        self.assertLess(time.monotonic() - started, 2.0)


class BranchReconcileTest(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.state_dir = self._tmp.name

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def test_reconcile_shows_git_branch_for_shell_pane(self) -> None:
        with _herdr(_snapshot([("w1:t1", "1", 1, "")], agent=None), branch="dev") as rename:
            daemon.reconcile_once(SOCK, self.state_dir)
        rename.assert_called_once_with(SOCK, "w1:t1", "proj ⎇ dev")

    def test_branch_lookup_skipped_for_agent_pane(self) -> None:
        with _herdr(_snapshot([("w1:t1", "1", 1, "Herdr eartag")]), branch="dev") as rename:
            daemon.reconcile_once(SOCK, self.state_dir)
        rename.assert_called_once_with(SOCK, "w1:t1", "Herdr eartag")


class PinReleaseTest(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.state_dir = self._tmp.name

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def test_pinned_tab_is_not_renamed(self) -> None:
        snap = _snapshot([("w1:t1", "1", 1, "Herdr eartag")])
        with _herdr(snap) as rename:
            self.assertEqual(daemon.pin_tab_now(SOCK, self.state_dir, "w1:t1"), "1")
            self.assertEqual(daemon.reconcile_once(SOCK, self.state_dir), 0)
        rename.assert_not_called()

    def test_release_hands_manual_tab_back(self) -> None:
        snap = _snapshot([("w1:t1", "배포 작업", 1, "Herdr eartag")])
        with _herdr(snap) as rename:
            self.assertEqual(daemon.release_tab_now(SOCK, self.state_dir, "w1:t1"), (False, 1))
        rename.assert_called_once_with(SOCK, "w1:t1", "Herdr eartag")

    def test_release_unpins_and_renames(self) -> None:
        snap = _snapshot([("w1:t1", "deploy", 1, "Herdr eartag")])
        with _herdr(snap) as rename:
            daemon.pin_tab_now(SOCK, self.state_dir, "w1:t1")
            self.assertEqual(daemon.release_tab_now(SOCK, self.state_dir, "w1:t1"), (True, 1))
        rename.assert_called_once_with(SOCK, "w1:t1", "Herdr eartag")

    def test_pin_survives_server_restart(self) -> None:
        # The pinned label is one the plugin set, so without the pin the restart fallback would manage it again.
        with _herdr(_snapshot([("w1:t1", "1", 1, "Herdr eartag")])):
            daemon.reconcile_once(SOCK, self.state_dir)
        with _herdr(_snapshot([("w1:t1", "Herdr eartag", 1, "Herdr eartag")])):
            daemon.pin_tab_now(SOCK, self.state_dir, "w1:t1")
        restarted = _snapshot([("w1:t5", "Herdr eartag", 1, "new title")])
        with _herdr(restarted, server="srv-2") as rename:
            self.assertEqual(daemon.reconcile_once(SOCK, self.state_dir), 0)
        rename.assert_not_called()

    def test_pin_unknown_tab_raises(self) -> None:
        with _herdr(_snapshot([])):
            with self.assertRaises(HerdrError):
                daemon.pin_tab_now(SOCK, self.state_dir, "w1:t9")


if __name__ == "__main__":
    unittest.main()
