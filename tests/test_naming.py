"""Tests for the pure tab label logic."""
from __future__ import annotations

import copy
import unittest
from typing import Optional

from eartag.naming import (
    MAX_LABEL_WIDTH,
    desired_label,
    display_width,
    folder_label,
    is_managed,
    needs_process_info,
    plan_renames,
    truncate_label,
)


def _proc_info(*procs: dict, leader: int = 1) -> dict:
    """Build a process_info reply."""
    return {"pane_id": "p", "foreground_process_group_id": leader, "foreground_processes": list(procs)}


ZSH = {"pid": 1, "name": "zsh", "argv0": "-zsh", "argv": ["-zsh"]}
VIM = {"pid": 1, "name": "vim", "argv0": "vim", "argv": ["vim", "a.py"]}


class DesiredLabelTest(unittest.TestCase):
    def test_claude_pane_uses_session_title(self) -> None:
        pane = {"agent": "claude", "terminal_title_stripped": "Herdr eartag", "foreground_cwd": "/a/b"}
        self.assertEqual(desired_label(pane), "Herdr eartag")

    def test_non_agent_pane_uses_folder(self) -> None:
        pane = {
            "agent": None,
            "terminal_title_stripped": "zsh",
            "foreground_cwd": "/work/herdr-eartag",
        }
        self.assertEqual(desired_label(pane), "herdr-eartag")

    def test_blank_title_falls_back_to_folder(self) -> None:
        pane = {"agent": "claude", "terminal_title_stripped": "   ", "foreground_cwd": "/a/proj"}
        self.assertEqual(desired_label(pane), "proj")

    def test_foreground_cwd_missing_uses_cwd(self) -> None:
        pane = {"agent": None, "terminal_title_stripped": "", "cwd": "/x/y/"}
        self.assertEqual(desired_label(pane), "y")

    def test_no_cwd_returns_none(self) -> None:
        # With no cwd info at all nothing can be computed → None (leave the tab alone)
        self.assertIsNone(desired_label({"agent": None}))


class FolderAndTruncateTest(unittest.TestCase):
    def test_root_path(self) -> None:
        self.assertEqual(folder_label("/"), "/")

    def test_display_width_and_truncate(self) -> None:
        self.assertEqual(display_width("가a"), 3)
        # Hangul takes 2 cells, so 20 characters (40 cells) must be cut to 30 cells or fewer.
        result = truncate_label("가" * 20)
        self.assertLessEqual(display_width(result), MAX_LABEL_WIDTH)
        self.assertTrue(result.endswith("…"))
        self.assertEqual(truncate_label("a\nb"), "a b")
        self.assertEqual(truncate_label("a" * 30), "a" * 30)
        self.assertEqual(display_width(truncate_label("a" * 31)), MAX_LABEL_WIDTH)

    def test_home_folder_is_tilde(self) -> None:
        self.assertEqual(folder_label("/home/u/", "/home/u"), "~")
        self.assertEqual(folder_label("/home/u/proj", "/home/u"), "proj")


class ActivityLabelTest(unittest.TestCase):
    """Label rules for cases other than Claude."""

    BASE = {"agent": None, "terminal_title_stripped": "", "foreground_cwd": "/w/proj"}

    def test_other_agent_with_title(self) -> None:
        self.assertEqual(desired_label({**self.BASE, "agent": "codex", "terminal_title_stripped": "fix bug"}), "fix bug")
        self.assertEqual(desired_label({**self.BASE, "agent": "codex"}), "codex · proj")

    def test_program_label(self) -> None:
        self.assertEqual(desired_label(self.BASE, _proc_info(VIM)), "vim · proj")

    def test_idle_shell_label(self) -> None:
        self.assertEqual(desired_label(self.BASE, _proc_info(ZSH)), "proj")

    def test_idle_shell_in_home(self) -> None:
        pane = {**self.BASE, "foreground_cwd": "/home/u"}
        self.assertEqual(desired_label(pane, _proc_info(ZSH), home="/home/u"), "~")

    def test_missing_process_info_falls_back_to_folder(self) -> None:
        self.assertEqual(desired_label(self.BASE, {"pane_id": "p"}), "proj")

    def test_ssh_label(self) -> None:
        ssh = {"pid": 1, "name": "ssh", "argv0": "ssh", "argv": ["ssh", "prod"]}
        info = _proc_info(ssh)
        self.assertEqual(desired_label({**self.BASE, "terminal_title_stripped": "ssh prod"}, info, local_host="mac"), "ssh · prod")
        self.assertEqual(
            desired_label({**self.BASE, "terminal_title_stripped": "u@prod: ~/app"}, info, local_host="mac"), "prod · app"
        )
        # A leftover local shell title is not mistaken for a remote folder.
        self.assertEqual(
            desired_label({**self.BASE, "terminal_title_stripped": "u@mac:/w"}, info, local_host="mac"), "ssh · prod"
        )
        bad = _proc_info({"pid": 1, "name": "ssh", "argv0": "ssh", "argv": ["ssh", "-V"]})
        self.assertEqual(desired_label(self.BASE, bad, local_host="mac"), "ssh · proj")

    def test_mosh_labels(self) -> None:
        perl_mosh = {"pid": 1, "name": "perl", "argv0": "perl", "argv": ["perl", "/usr/local/bin/mosh", "u@prod"]}
        self.assertEqual(desired_label(self.BASE, _proc_info(perl_mosh), local_host="mac"), "mosh · prod")
        client = {"pid": 1, "name": "mosh-client", "argv0": "mosh-client", "argv": ["mosh-client", "-#", "prod | 1.2.3.4 60001"]}
        self.assertEqual(desired_label(self.BASE, _proc_info(client), local_host="mac"), "mosh · prod")

    def test_needs_process_info(self) -> None:
        self.assertTrue(needs_process_info({"agent": None}))
        self.assertFalse(needs_process_info({"agent": "claude"}))


def _state(ws_labels: Optional[dict] = None, tabs: Optional[dict] = None) -> dict:
    """v3 state dict. ws_labels: {ws: [labels]}, tabs: {ws: {tab_id: [labels]}}."""
    workspaces: dict = {}
    for ws, labels in (ws_labels or {}).items():
        workspaces.setdefault(ws, {"labels": [], "tabs": {}})["labels"] = list(labels)
    for ws, records in (tabs or {}).items():
        workspaces.setdefault(ws, {"labels": [], "tabs": {}})["tabs"] = dict(records)
    return {"server": "srv", "workspaces": workspaces}


class IsManagedTest(unittest.TestCase):
    def test_default_label_is_position(self) -> None:
        # number is not used: the default label is the position number within the workspace.
        self.assertTrue(is_managed({"label": "1", "number": 1}, 1, None, []))
        self.assertTrue(is_managed({"label": "2", "number": 4}, 2, None, []))
        self.assertFalse(is_managed({"label": "4", "number": 4}, 2, None, []))

    def test_known_tab_uses_its_own_record_only(self) -> None:
        self.assertTrue(is_managed({"label": "proj"}, 1, ["old", "proj"], ["docs"]))
        # From review: if the user sets 'docs', a label the plugin used before, it must not be overwritten.
        self.assertFalse(is_managed({"label": "docs"}, 1, ["proj"], ["docs", "proj"]))

    def test_unknown_tab_falls_back_to_workspace_labels(self) -> None:
        # The workspace list is used only when there is no tab record (e.g. after a restart).
        self.assertTrue(is_managed({"label": "foo"}, 1, None, ["bar", "foo"]))
        self.assertFalse(is_managed({"label": "배포 작업"}, 1, None, ["foo"]))


def _snapshot() -> dict:
    """Minimal 3-tab snapshot: (a) default label + claude, (b) user-set name, (c) already the desired label."""
    return {
        "tabs": [
            {"tab_id": "w1:t1", "label": "1", "number": 1, "workspace_id": "w1"},
            {"tab_id": "w1:t2", "label": "배포 작업", "number": 2, "workspace_id": "w1"},
            {"tab_id": "w1:t3", "label": "proj", "number": 3, "workspace_id": "w1"},
        ],
        "layouts": [
            {"tab_id": "w1:t1", "focused_pane_id": "w1:p1"},
            {"tab_id": "w1:t2", "focused_pane_id": "w1:p2"},
            {"tab_id": "w1:t3", "focused_pane_id": "w1:p3"},
        ],
        "panes": [
            {"pane_id": "w1:p1", "agent": "claude", "terminal_title_stripped": "Herdr eartag", "foreground_cwd": "/a"},
            {"pane_id": "w1:p2", "agent": None, "terminal_title_stripped": "", "foreground_cwd": "/b/other"},
            {"pane_id": "w1:p3", "agent": None, "terminal_title_stripped": "", "foreground_cwd": "/c/proj"},
        ],
    }


class PlanRenamesTest(unittest.TestCase):
    def test_plan_renames_skips_manual_and_unchanged(self) -> None:
        managed = _state({"w1": ["old", "proj"]})
        self.assertEqual(plan_renames(_snapshot(), managed), [("w1:t1", "Herdr eartag")])

    def test_default_label_follows_position_not_number(self) -> None:
        # Observed: a tab created after closing another had number=4 but a position-based default label of "2".
        snapshot = {
            "tabs": [
                {"tab_id": "w11:t1", "label": "Herdr eartag", "number": 1, "workspace_id": "w11"},
                {"tab_id": "w11:t4", "label": "2", "number": 4, "workspace_id": "w11"},
                {"tab_id": "w12:t1", "label": "1", "number": 1, "workspace_id": "w12"},
            ],
            "layouts": [
                {"tab_id": "w11:t1", "focused_pane_id": "p1"},
                {"tab_id": "w11:t4", "focused_pane_id": "p4"},
                {"tab_id": "w12:t1", "focused_pane_id": "p5"},
            ],
            "panes": [
                {"pane_id": "p1", "agent": "claude", "terminal_title_stripped": "Herdr eartag", "foreground_cwd": "/a"},
                {"pane_id": "p4", "agent": "claude", "terminal_title_stripped": "새 세션", "foreground_cwd": "/a"},
                {"pane_id": "p5", "agent": None, "terminal_title_stripped": "", "foreground_cwd": "/x/proj"},
            ],
        }
        managed = _state({"w11": ["Herdr eartag"]})
        self.assertEqual(plan_renames(snapshot, managed), [("w11:t4", "새 세션"), ("w12:t1", "proj")])

    def test_tab_id_change_keeps_managed(self) -> None:
        # Even if tab IDs change after a Herdr restart, a label the plugin set in that workspace stays managed.
        snapshot = {
            "tabs": [
                {"tab_id": "w1:t1", "label": "1", "number": 1, "workspace_id": "w1"},
                {"tab_id": "w1:t9", "label": "Herdr eartag", "number": 9, "workspace_id": "w1"},
            ],
            "layouts": [{"tab_id": "w1:t9", "focused_pane_id": "p9"}],
            "panes": [{"pane_id": "p9", "agent": "claude", "terminal_title_stripped": "새 이름", "foreground_cwd": "/a"}],
        }
        self.assertEqual(plan_renames(snapshot, _state({"w1": ["Herdr eartag"]})), [("w1:t9", "새 이름")])

    def test_manual_name_matching_old_plugin_label_is_kept(self) -> None:
        # The tab record says 'proj' but the user set 'other', which matches an old plugin label → keep it
        snapshot = copy.deepcopy(_snapshot())
        snapshot["tabs"][2]["label"] = "other"
        state = _state({"w1": ["other", "proj"]}, {"w1": {"w1:t3": ["proj"]}})
        self.assertNotIn("w1:t3", [tab_id for tab_id, _ in plan_renames(snapshot, state)])

    def test_plan_renames_uses_process_info(self) -> None:
        snapshot = {
            "tabs": [{"tab_id": "w1:t1", "label": "1", "number": 1, "workspace_id": "w1"}],
            "layouts": [{"tab_id": "w1:t1", "focused_pane_id": "p1"}],
            "panes": [{"pane_id": "p1", "agent": None, "terminal_title_stripped": "", "foreground_cwd": "/w/proj"}],
        }
        self.assertEqual(plan_renames(snapshot, _state(), {"p1": _proc_info(VIM)}), [("w1:t1", "vim · proj")])

    def test_plan_renames_skips_tab_without_layout_or_pane(self) -> None:
        snapshot = {
            "tabs": [
                {"tab_id": "w1:t1", "label": "1", "number": 1, "workspace_id": "w1"},
                {"tab_id": "w1:t2", "label": "2", "number": 2, "workspace_id": "w1"},
            ],
            "layouts": [{"tab_id": "w1:t2", "focused_pane_id": "w1:p9"}],
            "panes": [],
        }
        self.assertEqual(plan_renames(snapshot, _state()), [])


if __name__ == "__main__":
    unittest.main()
