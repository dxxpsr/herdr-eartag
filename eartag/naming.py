"""Pure logic for computing tab labels.

Only functions with no external dependencies (sockets, files) live here. That way every rule
can be tested from a single snapshot dict, and the daemon only has to "read snapshot → call this module → apply".

Priority (highest first):
  pinned or user-set name > Claude session name > other agent > ssh/mosh session > running program > folder name
"""
from __future__ import annotations

import os
import unicodedata
from typing import Collection, Dict, List, NamedTuple, Optional, Tuple

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

# Agent identifier Herdr assigns to Claude panes
CLAUDE_AGENT = "claude"

# One-cell mark per tab agent status (tab.agent_status), shown as "<mark> <name>".
# Herdr shows agent status only in its sidebar, so the tab bar needs this to tell agent tabs apart.
# The glyphs are Herdr's own (sidebar with status_indicators = "symbols", observed in Herdr 0.9.5 as
# "× ◐ ✓ ○ ·" for blocked, working, done, idle, unknown), so the tab bar and the sidebar read the same.
# "unknown" (no agent in the tab) gets no mark rather than Herdr's "·", to keep plain tabs unchanged.
STATUS_MARKS: Dict[str, str] = {
    "working": "◐",
    "blocked": "×",
    "done": "✓",
    "idle": "○",
}
# Marks used by v0.5.0. Still stripped so tabs that carry them get the new mark instead of a stacked one.
PREVIOUS_MARKS = ("✻", "!", "✳")
_MARK_PREFIXES = tuple(f"{mark} " for mark in (*STATUS_MARKS.values(), *PREVIOUS_MARKS))


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


def with_status(name: str, status: Optional[str]) -> str:
    """Put the status mark in front of a name. Unknown status (no agent in the tab) → the name as is."""
    mark = STATUS_MARKS.get(status or "")
    return f"{mark} {name}" if mark else name


def strip_status(label: str) -> str:
    """The name without the plugin's status mark. Only one mark (plus its space) is ours."""
    for prefix in _MARK_PREFIXES:
        if label.startswith(prefix):
            return label[len(prefix):]
    return label


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
) -> Optional[str]:
    """Compute the desired tab label from pane state. None if there is nothing to go on (leave the tab alone)."""
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
        return _join(program, folder or "")

    return truncate_label(folder) if folder else None


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
    label = strip_status(tab.get("label") or "")
    if label == str(position):
        return True
    if record is not None:
        return label in record
    return label in workspace_labels


class TabPlan(NamedTuple):
    """What to do with one tab.

    Names (`*_name`) are labels without the status mark. Records hold names, not labels, so a mark that comes
    and goes with the agent status never makes a plugin-set label look like a user-set one.
    """

    tab_id: str
    workspace_id: str
    current: str  # label in the tab bar now
    desired: str  # label it should have (may equal current)
    managed: bool  # True: the plugin owns the name. False: pinned or user-set, only the mark is updated
    current_name: str
    desired_name: str


def plan_tabs(
    snapshot: dict,
    state: dict,
    process_infos: Optional[Dict[str, dict]] = None,
    home: Optional[str] = None,
    local_host: str = "",
) -> List[TabPlan]:
    """Compute the current and desired label for every tab.

    Unchanged tabs are returned too: the daemon must refresh their records every time, or a long-unchanged tab
    gets pushed out of the record and freezes as a "user-set name".
    Pinned and user-set tabs keep their name; only their status mark follows tab.agent_status.
    process_infos: focused pane ID → pane.process_info result (missing ones fall back to the folder name rule).
    Pinned tabs (pin action) are never managed, even when their label is the default position number.
    """
    process_infos = process_infos or {}
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
        status = tab.get("agent_status")
        current_name = strip_status(label)
        pinned = tab_id in (entry.get("pinned") or {})
        record = (entry.get("tabs") or {}).get(tab_id)
        managed = not pinned and is_managed(tab, positions[workspace_id], record, entry.get("labels") or [])
        if not managed:
            # Never shorten a name the user typed: the mark is just put in front of it.
            plans.append(
                TabPlan(tab_id, workspace_id, label, with_status(current_name, status), False, current_name, current_name)
            )
            continue
        # If the layout or pane does not exist yet (e.g. right after tab creation), keep the name for now
        # (the next event recomputes it) but still let the mark follow the status.
        pane_id = focused_by_tab.get(tab_id)
        pane = panes_by_id.get(pane_id)
        computed = None
        if pane is not None:
            computed = desired_label(pane, process_infos.get(pane_id or ""), home=home, local_host=local_host)
        desired_name = computed if computed is not None else current_name
        if STATUS_MARKS.get(status or ""):
            # Leave room for "<mark> " so the whole label stays within MAX_LABEL_WIDTH.
            desired_name = _cut_to_width(desired_name, MAX_LABEL_WIDTH - 2)
        plans.append(
            TabPlan(tab_id, workspace_id, label, with_status(desired_name, status), True, current_name, desired_name)
        )
    return plans


def plan_renames(
    snapshot: dict,
    state: dict,
    process_infos: Optional[Dict[str, dict]] = None,
    home: Optional[str] = None,
    local_host: str = "",
) -> List[Tuple[str, str]]:
    """The (tab_id, new_label) pairs to rename. Same-label renames are excluded since they only add events."""
    return [
        (plan.tab_id, plan.desired)
        for plan in plan_tabs(snapshot, state, process_infos, home, local_host)
        if plan.desired != plan.current
    ]
