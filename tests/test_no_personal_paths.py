"""Checks that no machine-specific info (username, hostname, home path, repo parent path) made it into the repo.

The personal tokens are looked up at run time, so this test file itself contains no personal info,
and on any machine it checks that *that machine's* info was not committed.
"""
from __future__ import annotations

import getpass
import os
import socket
import subprocess
import unittest
from typing import List, Set, Tuple

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
MIN_TOKEN_LEN = 3


def _personal_tokens() -> Set[str]:
    tokens: Set[str] = set()
    try:
        tokens.add(getpass.getuser())
    except Exception:  # noqa: BLE001 — the username may be unavailable in some environments
        pass
    tokens.add(socket.gethostname().split(".")[0])
    tokens.add(os.path.expanduser("~"))
    parent = os.path.dirname(ROOT)
    if parent != "/":
        tokens.add(parent)
    return {t for t in tokens if t and len(t) >= MIN_TOKEN_LEN}


def _tracked_files() -> List[str]:
    out = subprocess.run(
        ["git", "ls-files", "-z"], cwd=ROOT, capture_output=True, check=True, timeout=10
    ).stdout.decode("utf-8")
    return [f for f in out.split("\0") if f]


class NoPersonalInfoTest(unittest.TestCase):
    def test_tracked_files_have_no_personal_info(self) -> None:
        try:
            files = _tracked_files()
        except (OSError, subprocess.SubprocessError):
            self.skipTest("not a git repository, or git cannot be run")
        tokens = _personal_tokens()
        hits: List[Tuple[str, str]] = []
        for rel in files:
            try:
                with open(os.path.join(ROOT, rel), encoding="utf-8") as f:
                    text = f.read()
            except (OSError, UnicodeDecodeError):
                continue  # skip binary and deleted files
            hits.extend((rel, token) for token in sorted(tokens) if token in text)
        self.assertEqual(hits, [], "machine-specific info found in the repository")


if __name__ == "__main__":
    unittest.main()
