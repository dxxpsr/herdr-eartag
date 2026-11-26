"""Read the current git branch of a folder straight from the .git files.

Why not run `git`: the daemon recomputes every few seconds for every tab, and spawning a process per tab
adds up. Reading `.git/HEAD` is a couple of small file reads and keeps the plugin standard-library only.
Anything unexpected (no repo, unreadable file, odd format) returns None so the label just omits the branch.
"""
from __future__ import annotations

import os
from typing import Optional

# Characters of a detached HEAD commit hash to show (same as `git log --oneline`).
SHORT_HASH_LENGTH = 7
REF_PREFIX = "ref: "
BRANCH_REF_PREFIX = "refs/heads/"
GITDIR_PREFIX = "gitdir:"


def _read_first_line(path: str) -> Optional[str]:
    try:
        with open(path, encoding="utf-8", errors="replace") as f:
            return f.readline().strip()
    except OSError:
        return None


def _find_git_dir(start: str) -> Optional[str]:
    """Walk up from `start` to the nearest `.git`. A worktree's `.git` file is followed to the real git dir."""
    current = os.path.abspath(start)
    while True:
        candidate = os.path.join(current, ".git")
        if os.path.isdir(candidate):
            return candidate
        if os.path.isfile(candidate):
            line = _read_first_line(candidate)
            if not line or not line.startswith(GITDIR_PREFIX):
                return None
            target = line[len(GITDIR_PREFIX):].strip()
            # A relative gitdir is relative to the folder holding the .git file.
            return os.path.normpath(os.path.join(current, target))
        parent = os.path.dirname(current)
        if parent == current:
            return None
        current = parent


def current_branch(cwd: str) -> Optional[str]:
    """Branch name for the repo containing `cwd`, a short hash when detached, or None outside a repo."""
    if not cwd or not os.path.isdir(cwd):
        return None
    git_dir = _find_git_dir(cwd)
    if git_dir is None:
        return None
    head = _read_first_line(os.path.join(git_dir, "HEAD"))
    if not head:
        return None
    if head.startswith(REF_PREFIX):
        ref = head[len(REF_PREFIX):].strip()
        name = ref[len(BRANCH_REF_PREFIX):] if ref.startswith(BRANCH_REF_PREFIX) else ref
        return name or None
    # Detached HEAD: the file holds the commit hash itself.
    if all(c in "0123456789abcdef" for c in head.lower()) and len(head) >= SHORT_HASH_LENGTH:
        return head[:SHORT_HASH_LENGTH]
    return None
