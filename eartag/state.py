"""Store of "labels the plugin set".

This record is how a plugin-set name is told apart from a user-set name.

Format (v3):
  {
    "server": "server id",                        # used to tell whether the Herdr server changed (restart)
    "workspaces": {
      "w1": {
        "labels": ["label", ...],                 # labels the plugin set in this workspace (latest 50)
        "tabs": {"w1:t1": ["previous", "latest"]},  # the 2 latest labels the plugin set, per tab
        "pinned": {"w1:t2": "label"},               # tabs frozen by the pin action (optional key)
        "pinned_labels": ["label", ...]             # pins waiting to be matched again after a restart (optional key)
      }
    }
  }

Rules
- A known tab (has a record) is judged strictly by its own record, so a name the user sets by hand
  is not overwritten even if it matches an old plugin label.
- Tab records are cleared when the Herdr server changes (tab IDs may be reassigned after a restart). Then
  the workspace label list is used instead, so auto management does not freeze after a restart.
- Pinned tabs are never renamed, whatever their label (even the default position number).
  On a server change their IDs are no longer valid, so the pins become `pinned_labels` and are matched
  back to a live tab with the same label on the next recompute (adopt_pinned).
- Why 2 labels per tab: the new label is recorded right before the rename, so even if the rename fails or
  times out, the current label is still on record and the tab does not freeze.
"""
from __future__ import annotations

import json
import logging
import os
import tempfile
from typing import List, Optional, Set

logger = logging.getLogger(__name__)

# Labels to remember per workspace (used right after a restart). More labels mean more chances of misjudging.
MAX_LABELS_PER_WORKSPACE = 50
# Recent labels to remember per tab
MAX_LABELS_PER_TAB = 2
# Pins waiting for a restart match. A pin whose tab did not come back would otherwise linger forever.
MAX_PINNED_LABELS = 20

State = dict  # {"server": Optional[str], "workspaces": Dict[str, {"labels": List[str], "tabs": Dict[str, List[str]]}]}


def empty_state() -> State:
    return {"server": None, "workspaces": {}}


def _str_list(value: object) -> List[str]:
    return [v for v in value if isinstance(v, str)] if isinstance(value, list) else []


def _workspace(state: State, workspace_id: str) -> dict:
    return state["workspaces"].setdefault(workspace_id, {"labels": [], "tabs": {}})


def _normalize(data: dict) -> State:
    """Bring v1 ({tab_id: label}), v2 ({ws: [labels]}) and v3 formats into v3. Invalid entries are dropped."""
    state = empty_state()
    if "workspaces" in data:  # v3
        server = data.get("server")
        state["server"] = server if isinstance(server, str) else None
        workspaces = data.get("workspaces")
        for ws, entry in (workspaces.items() if isinstance(workspaces, dict) else []):
            if not isinstance(ws, str) or not isinstance(entry, dict):
                continue
            tabs_raw = entry.get("tabs")
            tabs = {
                tab_id: _str_list(labels)[-MAX_LABELS_PER_TAB:]
                for tab_id, labels in (tabs_raw.items() if isinstance(tabs_raw, dict) else [])
                if isinstance(tab_id, str) and _str_list(labels)
            }
            normalized: dict = {"labels": _str_list(entry.get("labels"))[-MAX_LABELS_PER_WORKSPACE:], "tabs": tabs}
            pinned_raw = entry.get("pinned")
            pinned = {
                tab_id: label
                for tab_id, label in (pinned_raw.items() if isinstance(pinned_raw, dict) else [])
                if isinstance(tab_id, str) and isinstance(label, str)
            }
            if pinned:
                normalized["pinned"] = pinned
            pinned_labels = _str_list(entry.get("pinned_labels"))[-MAX_PINNED_LABELS:]
            if pinned_labels:
                normalized["pinned_labels"] = pinned_labels
            state["workspaces"][ws] = normalized
        return state

    for key, value in data.items():
        if not isinstance(key, str):
            continue
        if isinstance(value, str) and ":" in key:  # v1: tab ID → label
            remember_label(state, key.split(":", 1)[0], key, value)
        elif isinstance(value, list):  # v2: workspace → label list
            for label in _str_list(value):
                _remember_workspace_label(_workspace(state, key), label)
    return state


def load_state(path: str) -> State:
    """Read the state file. If it is missing or broken, start empty (so the daemon does not stop)."""
    try:
        with open(path, encoding="utf-8") as f:
            data = json.load(f)
    except FileNotFoundError:
        return empty_state()
    except (OSError, ValueError) as exc:
        logger.warning("Could not read the state file, starting empty: %s (%s)", path, exc)
        return empty_state()
    if not isinstance(data, dict):
        logger.warning("State file has an invalid format, starting empty: %s", path)
        return empty_state()
    return _normalize(data)


def save_state(path: str, state: State) -> None:
    """Write to a temp file, then swap it in with os.replace (so the file is never corrupted by an exit mid-write)."""
    directory = os.path.dirname(path) or "."
    os.makedirs(directory, exist_ok=True)
    fd, tmp_path = tempfile.mkstemp(dir=directory, prefix=".labels-", suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(state, f, ensure_ascii=False, indent=1)
        os.replace(tmp_path, path)
    except BaseException:
        # If the swap fails, remove the temp file so it is not left behind.
        try:
            os.unlink(tmp_path)
        except OSError:
            pass
        raise


def _remember_workspace_label(entry: dict, label: str) -> None:
    # Numeric labels are handled by the position-number rule, so keep them out of the list (avoids misjudging user-set numeric names).
    if label.isdigit():
        return
    labels = [existing for existing in entry["labels"] if existing != label]
    labels.append(label)
    entry["labels"] = labels[-MAX_LABELS_PER_WORKSPACE:]


def remember_label(state: State, workspace_id: str, tab_id: str, label: str) -> None:
    """Record a label the plugin set (or is about to set) in the tab record and the workspace list. Latest goes last."""
    entry = _workspace(state, workspace_id)
    _remember_workspace_label(entry, label)
    record = [existing for existing in entry["tabs"].get(tab_id, []) if existing != label]
    record.append(label)
    entry["tabs"][tab_id] = record[-MAX_LABELS_PER_TAB:]


def tab_record(state: State, workspace_id: str, tab_id: str) -> Optional[List[str]]:
    """That tab's record. None for a tab with no record (an unknown tab)."""
    entry = state["workspaces"].get(workspace_id)
    return entry["tabs"].get(tab_id) if entry else None


def workspace_labels(state: State, workspace_id: str) -> List[str]:
    entry = state["workspaces"].get(workspace_id)
    return entry["labels"] if entry else []


def begin_session(state: State, server_id: Optional[str]) -> None:
    """Clear tab records if the Herdr server changed (restart). Leave them alone if the server id is unknown.

    Pins are keyed by tab ID too, so they are turned into labels to be matched again (adopt_pinned).
    """
    if server_id is None or state.get("server") == server_id:
        return
    for entry in state["workspaces"].values():
        entry["tabs"] = {}
        pinned = entry.pop("pinned", None) or {}
        if pinned:
            waiting = list(entry.get("pinned_labels") or []) + list(pinned.values())
            entry["pinned_labels"] = waiting[-MAX_PINNED_LABELS:]
    state["server"] = server_id


def pin_tab(state: State, workspace_id: str, tab_id: str, label: str) -> None:
    """Freeze a tab: the daemon leaves it alone until it is released."""
    _workspace(state, workspace_id).setdefault("pinned", {})[tab_id] = label


def unpin_tab(state: State, workspace_id: str, tab_id: str) -> bool:
    """Release a pinned tab. False if it was not pinned."""
    entry = state["workspaces"].get(workspace_id)
    pinned = entry.get("pinned") if entry else None
    if not pinned or tab_id not in pinned:
        return False
    del pinned[tab_id]
    return True


def is_pinned(state: State, workspace_id: str, tab_id: str) -> bool:
    entry = state["workspaces"].get(workspace_id)
    return bool(entry) and tab_id in (entry.get("pinned") or {})


def refresh_pinned(state: State, tabs: List[dict]) -> None:
    """Keep each pin's label equal to the tab's current label (the user may rename a pinned tab).

    The label is the only thing that can find the tab again after a restart, so a stale one would lose the pin.
    """
    for tab in tabs:
        entry = state["workspaces"].get(tab.get("workspace_id") or "")
        pinned = entry.get("pinned") if entry else None
        tab_id, label = tab.get("tab_id"), tab.get("label")
        if pinned and tab_id in pinned and isinstance(label, str):
            pinned[tab_id] = label


def adopt_pinned(state: State, tabs: List[dict]) -> None:
    """After a restart, pin the live tabs whose label matches a waiting pin. Each waiting pin is used once.

    Tabs are visited in tab bar order, so with two same-named tabs the leftmost one gets the pin.
    """
    for tab in tabs:
        entry = state["workspaces"].get(tab.get("workspace_id") or "")
        waiting = entry.get("pinned_labels") if entry else None
        tab_id, label = tab.get("tab_id"), tab.get("label")
        if not waiting or not isinstance(tab_id, str) or label not in waiting:
            continue
        if tab_id in (entry.get("pinned") or {}):
            continue
        waiting.remove(label)
        entry.setdefault("pinned", {})[tab_id] = label


def prune_state(state: State, live_workspace_ids: Set[str], live_tab_ids: Set[str]) -> None:
    """Drop records for closed workspaces and tabs (in place)."""
    state["workspaces"] = {ws: entry for ws, entry in state["workspaces"].items() if ws in live_workspace_ids}
    for entry in state["workspaces"].values():
        entry["tabs"] = {tab_id: labels for tab_id, labels in entry["tabs"].items() if tab_id in live_tab_ids}
        if "pinned" in entry:
            entry["pinned"] = {tab_id: label for tab_id, label in entry["pinned"].items() if tab_id in live_tab_ids}
