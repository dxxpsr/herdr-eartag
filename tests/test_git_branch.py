"""Tests for reading the current git branch straight from the .git files."""
from __future__ import annotations

import os
import tempfile
import unittest

from eartag.git_branch import current_branch


def _write(path: str, content: str) -> None:
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        f.write(content)


class CurrentBranchTest(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.root = os.path.realpath(self._tmp.name)

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def test_branch_from_repo_root(self) -> None:
        _write(os.path.join(self.root, "repo", ".git", "HEAD"), "ref: refs/heads/fix-login\n")
        self.assertEqual(current_branch(os.path.join(self.root, "repo")), "fix-login")

    def test_branch_from_subfolder(self) -> None:
        _write(os.path.join(self.root, "repo", ".git", "HEAD"), "ref: refs/heads/main\n")
        sub = os.path.join(self.root, "repo", "src", "app")
        os.makedirs(sub)
        self.assertEqual(current_branch(sub), "main")

    def test_branch_name_with_slash(self) -> None:
        _write(os.path.join(self.root, "repo", ".git", "HEAD"), "ref: refs/heads/feature/login\n")
        self.assertEqual(current_branch(os.path.join(self.root, "repo")), "feature/login")

    def test_detached_head_shows_short_hash(self) -> None:
        _write(os.path.join(self.root, "repo", ".git", "HEAD"), "3ba19db0c1d2e3f4a5b6c7d8e9f0a1b2c3d4e5f6\n")
        self.assertEqual(current_branch(os.path.join(self.root, "repo")), "3ba19db")

    def test_worktree_git_file_relative_gitdir(self) -> None:
        # In a worktree, .git is a file pointing at the real git dir.
        gitdir = os.path.join(self.root, "main-repo", ".git", "worktrees", "wt")
        _write(os.path.join(gitdir, "HEAD"), "ref: refs/heads/new-ui\n")
        _write(os.path.join(self.root, "wt", ".git"), "gitdir: ../main-repo/.git/worktrees/wt\n")
        self.assertEqual(current_branch(os.path.join(self.root, "wt")), "new-ui")

    def test_worktree_git_file_absolute_gitdir(self) -> None:
        gitdir = os.path.join(self.root, "main-repo", ".git", "worktrees", "wt")
        _write(os.path.join(gitdir, "HEAD"), "ref: refs/heads/new-ui\n")
        _write(os.path.join(self.root, "wt", ".git"), f"gitdir: {gitdir}\n")
        self.assertEqual(current_branch(os.path.join(self.root, "wt")), "new-ui")

    def test_not_a_repo_returns_none(self) -> None:
        plain = os.path.join(self.root, "plain")
        os.makedirs(plain)
        self.assertIsNone(current_branch(plain))

    def test_missing_folder_returns_none(self) -> None:
        self.assertIsNone(current_branch(os.path.join(self.root, "gone")))

    def test_broken_git_file_returns_none(self) -> None:
        _write(os.path.join(self.root, "wt", ".git"), "garbage\n")
        self.assertIsNone(current_branch(os.path.join(self.root, "wt")))

    def test_unreadable_head_returns_none(self) -> None:
        os.makedirs(os.path.join(self.root, "repo", ".git"))  # .git without HEAD
        self.assertIsNone(current_branch(os.path.join(self.root, "repo")))


if __name__ == "__main__":
    unittest.main()
