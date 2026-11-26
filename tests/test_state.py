"""Tests for the "labels the plugin set" store.

Format (v3): {"server": server id, "workspaces": {ws: {"labels": [...], "tabs": {tab_id: [latest labels ≤2]}}}}
"""
from __future__ import annotations

import json
import os
import tempfile
import unittest

from eartag.state import (
    MAX_LABELS_PER_WORKSPACE,
    adopt_pinned,
    begin_session,
    is_pinned,
    load_state,
    pin_tab,
    prune_state,
    refresh_pinned,
    remember_label,
    save_state,
    tab_record,
    unpin_tab,
    workspace_labels,
)


def _ws(state: dict, ws: str) -> dict:
    return state["workspaces"][ws]


class StateFileTest(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.path = os.path.join(self._tmp.name, "labels.json")

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def _write(self, data: object) -> None:
        with open(self.path, "w", encoding="utf-8") as f:
            f.write(data if isinstance(data, str) else json.dumps(data, ensure_ascii=False))

    def test_roundtrip(self) -> None:
        state = load_state(self.path)
        remember_label(state, "w1", "w1:t1", "한글 라벨")
        save_state(self.path, state)
        self.assertEqual(load_state(self.path), state)

    def test_missing_corrupt_or_non_dict_returns_empty(self) -> None:
        empty = {"server": None, "workspaces": {}}
        self.assertEqual(load_state(self.path), empty)
        self._write("{not json")
        self.assertEqual(load_state(self.path), empty)
        self._write("[1,2]")
        self.assertEqual(load_state(self.path), empty)

    def test_load_migrates_v1_format(self) -> None:
        self._write({"w1:t1": "a", "w1:t2": "b", "w2:t1": "c"})
        state = load_state(self.path)
        self.assertEqual(workspace_labels(state, "w1"), ["a", "b"])
        self.assertEqual(tab_record(state, "w1", "w1:t2"), ["b"])
        self.assertEqual(workspace_labels(state, "w2"), ["c"])

    def test_load_migrates_v2_format(self) -> None:
        self._write({"w1": ["a", "b", 3]})
        state = load_state(self.path)
        self.assertEqual(workspace_labels(state, "w1"), ["a", "b"])
        self.assertIsNone(tab_record(state, "w1", "w1:t1"))

    def test_load_drops_invalid_v3_entries(self) -> None:
        self._write({"server": 5, "workspaces": {"w1": {"labels": ["a", None], "tabs": {"w1:t1": ["a", 1], "x": "bad"}}, "w2": "bad"}})
        state = load_state(self.path)
        self.assertEqual(state["server"], None)
        self.assertEqual(state["workspaces"], {"w1": {"labels": ["a"], "tabs": {"w1:t1": ["a"]}}})


class StateOpsTest(unittest.TestCase):
    def setUp(self) -> None:
        self.state = {"server": None, "workspaces": {}}

    def test_remember_label_caps_workspace_list_and_keeps_two_per_tab(self) -> None:
        for i in range(60):
            remember_label(self.state, "w1", "w1:t1", f"L{i}")
        self.assertEqual(len(workspace_labels(self.state, "w1")), MAX_LABELS_PER_WORKSPACE)
        self.assertEqual(workspace_labels(self.state, "w1")[-1], "L59")
        self.assertEqual(tab_record(self.state, "w1", "w1:t1"), ["L58", "L59"])
        remember_label(self.state, "w1", "w1:t1", "L58")
        self.assertEqual(tab_record(self.state, "w1", "w1:t1"), ["L59", "L58"])

    def test_remember_label_skips_digits_in_workspace_list(self) -> None:
        # Numeric labels are handled by the position-number rule → kept out of the workspace list.
        remember_label(self.state, "w1", "w1:t1", "2")
        self.assertEqual(workspace_labels(self.state, "w1"), [])
        self.assertEqual(tab_record(self.state, "w1", "w1:t1"), ["2"])

    def test_begin_session_clears_tab_records_on_server_change(self) -> None:
        remember_label(self.state, "w1", "w1:t1", "a")
        begin_session(self.state, "srv-1")  # previous server unknown (None) → tab records cleared
        self.assertIsNone(tab_record(self.state, "w1", "w1:t1"))
        remember_label(self.state, "w1", "w1:t1", "b")
        begin_session(self.state, "srv-1")  # same server → kept
        self.assertEqual(tab_record(self.state, "w1", "w1:t1"), ["b"])
        begin_session(self.state, "srv-2")  # server restarted → tab IDs may have changed, so cleared
        self.assertIsNone(tab_record(self.state, "w1", "w1:t1"))
        self.assertEqual(workspace_labels(self.state, "w1"), ["a", "b"])  # the workspace list is kept

    def test_begin_session_without_server_id_keeps_records(self) -> None:
        remember_label(self.state, "w1", "w1:t1", "a")
        begin_session(self.state, None)
        self.assertEqual(tab_record(self.state, "w1", "w1:t1"), ["a"])

    def test_prune(self) -> None:
        remember_label(self.state, "w1", "w1:t1", "a")
        remember_label(self.state, "w1", "w1:t2", "b")
        remember_label(self.state, "w2", "w2:t1", "c")
        prune_state(self.state, {"w1"}, {"w1:t1"})
        self.assertEqual(list(self.state["workspaces"]), ["w1"])
        self.assertIsNone(tab_record(self.state, "w1", "w1:t2"))
        self.assertEqual(workspace_labels(self.state, "w1"), ["a", "b"])


class PinStateTest(unittest.TestCase):
    """Pinned tabs: frozen names the daemon never touches, surviving a Herdr restart."""

    def setUp(self) -> None:
        self.state = {"server": "srv-1", "workspaces": {}}

    def test_pin_and_unpin(self) -> None:
        pin_tab(self.state, "w1", "w1:t1", "deploy")
        self.assertTrue(is_pinned(self.state, "w1", "w1:t1"))
        self.assertTrue(unpin_tab(self.state, "w1", "w1:t1"))
        self.assertFalse(is_pinned(self.state, "w1", "w1:t1"))
        self.assertFalse(unpin_tab(self.state, "w1", "w1:t1"))  # already released → reports nothing changed

    def test_unpin_unknown_workspace(self) -> None:
        self.assertFalse(unpin_tab(self.state, "w9", "w9:t1"))

    def test_refresh_pinned_follows_current_label(self) -> None:
        # The label is what lets a pinned tab be found again after a restart, so keep it current.
        pin_tab(self.state, "w1", "w1:t1", "old")
        refresh_pinned(self.state, [{"tab_id": "w1:t1", "workspace_id": "w1", "label": "new"}])
        self.assertEqual(_ws(self.state, "w1")["pinned"], {"w1:t1": "new"})

    def test_restart_moves_pins_to_labels_and_adopts_them(self) -> None:
        pin_tab(self.state, "w1", "w1:t1", "deploy")
        begin_session(self.state, "srv-2")
        self.assertFalse(is_pinned(self.state, "w1", "w1:t1"))  # old IDs may now belong to other tabs
        tabs = [
            {"tab_id": "w1:t7", "workspace_id": "w1", "label": "other"},
            {"tab_id": "w1:t8", "workspace_id": "w1", "label": "deploy"},
            {"tab_id": "w1:t9", "workspace_id": "w1", "label": "deploy"},
        ]
        adopt_pinned(self.state, tabs)
        self.assertTrue(is_pinned(self.state, "w1", "w1:t8"))
        # One saved pin is consumed by one tab, so a same-named second tab is not pinned too.
        self.assertFalse(is_pinned(self.state, "w1", "w1:t9"))
        self.assertFalse(is_pinned(self.state, "w1", "w1:t7"))
        self.assertEqual(_ws(self.state, "w1").get("pinned_labels"), [])

    def test_prune_drops_pins_of_closed_tabs(self) -> None:
        pin_tab(self.state, "w1", "w1:t1", "a")
        pin_tab(self.state, "w1", "w1:t2", "b")
        prune_state(self.state, {"w1"}, {"w1:t1"})
        self.assertEqual(_ws(self.state, "w1")["pinned"], {"w1:t1": "a"})

    def test_pins_survive_save_and_load(self) -> None:
        pin_tab(self.state, "w1", "w1:t1", "a")
        self.state["workspaces"]["w1"]["pinned_labels"] = ["b"]
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "labels.json")
            save_state(path, self.state)
            loaded = load_state(path)
        self.assertEqual(_ws(loaded, "w1")["pinned"], {"w1:t1": "a"})
        self.assertEqual(_ws(loaded, "w1")["pinned_labels"], ["b"])


if __name__ == "__main__":
    unittest.main()
