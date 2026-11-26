"""Pure logic for computing tab labels.

Only functions with no external dependencies (sockets, files) live here. That way every rule
can be tested from a single snapshot dict, and the daemon only has to "read snapshot → call this module → apply".

Priority (highest first):
  pinned or user-set name > Claude session name > other agent > ssh/mosh session > running program > folder name
"""
from __future__ import annotations

import os
import unicodedata
from typing import Collection, Dict, List, Optional, Tuple

from .activity import (
    REMOTE_PROGRAMS,
    command_argv,
    is_shell,
    leader_process,
    program_name,
    remote_display_name,
    remote_location,
    ssh_target,
)

# Cap labels by on-screen width (cells) so the tab bar does not overflow. Wide characters such as Hangul take 2 cells.
MAX_LABEL_WIDTH: int = 30
ELLIPSIS = "…"
SEPARATOR = " · "
# Branch marker between the folder and the branch name (1 cell wide).
BRANCH_SEPARATOR = " ⎇ "
# Cap the branch part on its own so a long branch name does not push the folder name out of the label.
MAX_BRANCH_WIDTH: int = 12
# Branches that are the usual state of a repo. Showing them only costs width, so they are omitted.
DEFAULT_BRANCHES = frozenset({"main", "master"})

# Agent identifier Herdr assigns to Claude panes
CLAUDE_AGENT = "claude"


def _char_width(char: str) -> int:
    if unicodedata.combining(char):
        return 0
    return 2 if unicodedata.east_asian_width(char) in ("W", "F") else 1


def display_width(text: str) -> int:
    """Number of cells the text takes on a terminal screen."""
    return sum(_char_width(c) for c in text)


def truncate_label(text: str) -> str:
    """Turn newlines and tabs into spaces, trim both ends, then cut to the on-screen width."""
    # Control characters in a terminal title can break the tab bar layout, so replace them with spaces.
    cleaned = " ".join(text.replace("\t", " ").splitlines()).strip()
    return _cut_to_width(cleaned, MAX_LABEL_WIDTH)


def _cut_to_width(cleaned: str, max_width: int) -> str:
    """Cut text to `max_width` cells, ending with an ellipsis when something was dropped."""
    if display_width(cleaned) <= max_width:
        return cleaned
    budget = max_width - display_width(ELLIPSIS)
    kept: List[str] = []
    used = 0
    for char in cleaned:
        width = _char_width(char)
        if used + width > budget:
            break
        kept.append(char)
        used += width
    return "".join(kept).rstrip() + ELLIPSIS


def folder_label(path: str, home: Optional[str] = None) -> str:
    """The last folder name in a path. `/` for the root, `~` for the home folder."""
    trimmed = path.rstrip("/")
    if home and trimmed == home.rstrip("/"):
        return "~"
    return os.path.basename(trimmed) or "/"


def needs_process_info(pane: dict) -> bool:
    """Look up the running program only for panes without an agent (saves socket requests)."""
    return not pane.get("agent")


def _join(*parts: str) -> str:
    return truncate_label(SEPARATOR.join(p for p in parts if p))


def _with_branch(folder: Optional[str], branch: Optional[str]) -> str:
    """`folder ⎇ branch`. The branch is dropped outside a repo, on a default branch, or with no folder."""
    if not folder:
        return ""
    if not branch or branch in DEFAULT_BRANCHES:
        return folder
    cleaned = " ".join(branch.split())  # a branch name has no whitespace, but stay safe for the tab bar
    return folder + BRANCH_SEPARATOR + _cut_to_width(cleaned, MAX_BRANCH_WIDTH)


def _remote_label(program: str, argv: List[str], title: str, folder: Optional[str], local_host: str) -> str:
    """ssh/mosh label: `server · remote folder` if the remote location is known, otherwise `ssh · server`."""
    target = ssh_target(program, argv)
    remote = remote_location(title, local_host)
    if remote:
        host, remote_folder = remote
        return _join(target or host, remote_folder)
    # If no server can be extracted (e.g. ssh -V), fall back to the local folder.
    return _join(remote_display_name(program), target or folder or "")


def desired_label(
    pane: dict,
    process_info: Optional[dict] = None,
    home: Optional[str] = None,
    local_host: str = "",
    branch: Optional[str] = None,
) -> Optional[str]:
    """Compute the desired tab label from pane state. None if there is nothing to go on (leave the tab alone).

    branch: git branch of the pane's folder (None outside a repo). Only shell and program labels show it:
    an agent title already says what the tab is about, and an ssh label is about the remote side.
    """
    # A cd inside the shell shows up in foreground_cwd, so prefer it.
    cwd = pane.get("foreground_cwd") or pane.get("cwd")
    folder = folder_label(cwd, home) if cwd else None
    title = truncate_label(pane.get("terminal_title_stripped") or "")
    agent = pane.get("agent")

    if agent == CLAUDE_AGENT:
        # Claude puts its session name (auto title or /rename name) in the terminal title.
        if title:
            return title
    elif agent:
        # Other agents: the title if there is one, otherwise at least show which agent it is.
        return title or _join(str(agent), folder or "")

    leader = leader_process(process_info)
    program = program_name(leader) if leader else None
    if program and not is_shell(program):
        if program in REMOTE_PROGRAMS:
            argv = command_argv(leader) if leader else []
            return _remote_label(program, argv, pane.get("terminal_title_stripped") or "", folder, local_host)
        return _join(program, _with_branch(folder, branch))

    return truncate_label(_with_branch(folder, branch)) if folder else None


def is_managed(tab: dict, position: int, record: Optional[Collection[str]], workspace_labels: Collection[str]) -> bool:
    """Decide whether the plugin manages this tab.

    Herdr tab info has no "user-set name" flag, so a tab counts as managed only when:
    ① it has the default label (its position number in the workspace, from 1) — renaming a tab to its position number hands it back to auto management
    ② known tab (record exists): its label is one of the recent labels the plugin set on that tab
    ③ unknown tab (no record, e.g. right after a Herdr restart): its label is one the plugin set in that workspace
    Anything else is treated as a user-set name and left alone.

    Note: the tab's `number` field is not the default label. A tab created after closing another
    was observed with number=4 while its position-based default label was "2" (Herdr 0.9.5).
    """
    label = tab.get("label")
    if label == str(position):
        return True
    if record is not None:
        return label in record
    return label in workspace_labels


# (tab_id, workspace_id, current label, desired label or None) — one per managed tab
TabPlan = Tuple[str, str, str, Optional[str]]


def plan_tabs(
    snapshot: dict,
    state: dict,
    process_infos: Optional[Dict[str, dict]] = None,
    home: Optional[str] = None,
    local_host: str = "",
    branches: Optional[Dict[str, str]] = None,
) -> List[TabPlan]:
    """Compute the current and desired label for every managed tab.

    Unchanged tabs are returned too: the daemon must refresh their records every time, or a long-unchanged tab
    gets pushed out of the record and freezes as a "user-set name".
    process_infos: focused pane ID → pane.process_info result (missing ones fall back to the folder name rule).
    branches: focused pane ID → git branch of its folder (missing means no branch is shown).
    Pinned tabs (pin action) are skipped entirely, even when their label is the default position number.
    """
    process_infos = process_infos or {}
    branches = branches or {}
    workspaces = state.get("workspaces", {})
    # Build lookup dicts once instead of scanning linearly per tab.
    panes_by_id = {p.get("pane_id"): p for p in snapshot.get("panes", [])}
    focused_by_tab = {l.get("tab_id"): l.get("focused_pane_id") for l in snapshot.get("layouts", [])}

    plans: List[TabPlan] = []
    # Snapshot tab order = tab bar display order. Count positions from 1 per workspace.
    # Observed (Herdr 0.9.5): moving an unnamed tab to the front with tab.move changes both the list order and
    # the default label ("3" → "1"), so the position counted from list order matches the default label.
    positions: Dict[str, int] = {}
    for tab in snapshot.get("tabs", []):
        workspace_id = tab.get("workspace_id") or ""
        positions[workspace_id] = positions.get(workspace_id, 0) + 1
        tab_id = tab.get("tab_id")
        label = tab.get("label")
        if not tab_id or not isinstance(label, str):
            continue
        entry = workspaces.get(workspace_id) or {}
        if tab_id in (entry.get("pinned") or {}):
            continue
        record = (entry.get("tabs") or {}).get(tab_id)
        if not is_managed(tab, positions[workspace_id], record, entry.get("labels") or []):
            continue
        # If the layout or pane does not exist yet (e.g. right after tab creation), recompute on the next event.
        pane_id = focused_by_tab.get(tab_id)
        pane = panes_by_id.get(pane_id)
        if pane is None:
            continue
        desired = desired_label(
            pane,
            process_infos.get(pane_id or ""),
            home=home,
            local_host=local_host,
            branch=branches.get(pane_id or ""),
        )
        plans.append((tab_id, workspace_id, label, desired))
    return plans


def plan_renames(
    snapshot: dict,
    state: dict,
    process_infos: Optional[Dict[str, dict]] = None,
    home: Optional[str] = None,
    local_host: str = "",
    branches: Optional[Dict[str, str]] = None,
) -> List[Tuple[str, str]]:
    """The (tab_id, new_label) pairs to rename. Same-label renames are excluded since they only add events."""
    return [
        (tab_id, desired)
        for tab_id, _ws, current, desired in plan_tabs(snapshot, state, process_infos, home, local_host, branches)
        if desired is not None and desired != current
    ]
