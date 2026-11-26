"""Tests for the main.py entry point. Runs it as a real process and checks exit codes and messages."""
from __future__ import annotations

import os
import subprocess
import sys
import tempfile
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
MAIN = os.path.join(ROOT, "main.py")


def _run(args: list, env: dict) -> subprocess.CompletedProcess:
    return subprocess.run(
        [sys.executable, MAIN, *args], env=env, capture_output=True, text=True, timeout=20, cwd=ROOT
    )


class MainTest(unittest.TestCase):
    def test_missing_env_exits_zero(self) -> None:
        result = _run(["resync"], {"PATH": os.environ.get("PATH", "")})
        self.assertEqual(result.returncode, 0)
        self.assertIn("HERDR_SOCKET_PATH", result.stderr)

    def test_resync_with_missing_socket_exits_zero(self) -> None:
        with tempfile.TemporaryDirectory() as state_dir:
            env = {
                "PATH": os.environ.get("PATH", ""),
                "HERDR_SOCKET_PATH": "/tmp/does-not-exist-eartag.sock",
                "HERDR_PLUGIN_STATE_DIR": state_dir,
            }
            result = _run(["resync"], env)
        self.assertEqual(result.returncode, 0)
        self.assertIn("Cannot connect to the Herdr socket", result.stdout + result.stderr)

    def test_status_without_daemon(self) -> None:
        with tempfile.TemporaryDirectory() as state_dir:
            env = {
                "PATH": os.environ.get("PATH", ""),
                "HERDR_SOCKET_PATH": "/tmp/does-not-exist-eartag.sock",
                "HERDR_PLUGIN_STATE_DIR": state_dir,
            }
            result = _run(["status"], env)
        self.assertEqual(result.returncode, 0)
        self.assertIn("not running", result.stdout)

    def test_stop_without_daemon(self) -> None:
        with tempfile.TemporaryDirectory() as state_dir:
            env = {
                "PATH": os.environ.get("PATH", ""),
                "HERDR_SOCKET_PATH": "/tmp/does-not-exist-eartag.sock",
                "HERDR_PLUGIN_STATE_DIR": state_dir,
            }
            result = _run(["stop"], env)
        self.assertEqual(result.returncode, 0)
        self.assertIn("No daemon running", result.stdout)

    def test_start_with_missing_socket(self) -> None:
        with tempfile.TemporaryDirectory() as state_dir:
            env = {
                "PATH": os.environ.get("PATH", ""),
                "HERDR_SOCKET_PATH": "/tmp/does-not-exist-eartag.sock",
                "HERDR_PLUGIN_STATE_DIR": state_dir,
            }
            result = _run(["start"], env)
            # If a daemon had been launched, a lock file would exist in the state dir.
            spawned = [f for f in os.listdir(state_dir) if f.startswith("daemon-")]
        self.assertEqual(result.returncode, 0)
        self.assertIn("Herdr socket", result.stdout)
        self.assertEqual(spawned, [])

    def test_stop_when_pid_unreadable(self) -> None:
        # If the lock is held but the pid cannot be read, report it as unreadable rather than "no daemon".
        import fcntl

        socket_path = "/tmp/does-not-exist-eartag.sock"
        with tempfile.TemporaryDirectory() as state_dir:
            sys.path.insert(0, ROOT)
            from eartag import daemon

            lock = daemon.lock_path(state_dir, socket_path)
            with open(lock, "w", encoding="utf-8") as handle:
                handle.write("garbage")
                handle.flush()
                fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
                env = {"PATH": os.environ.get("PATH", ""), "HERDR_SOCKET_PATH": socket_path, "HERDR_PLUGIN_STATE_DIR": state_dir}
                result = _run(["stop"], env)
        self.assertEqual(result.returncode, 0)
        self.assertIn("pid could not be read", result.stdout)

    def test_unknown_command_exits_nonzero(self) -> None:
        result = _run(["nope"], {"PATH": os.environ.get("PATH", "")})
        self.assertNotEqual(result.returncode, 0)


    def test_pin_and_release_with_missing_socket_exit_zero(self) -> None:
        for command in ("pin", "release"):
            with tempfile.TemporaryDirectory() as state_dir:
                env = {
                    "PATH": os.environ.get("PATH", ""),
                    "HERDR_SOCKET_PATH": "/tmp/does-not-exist-eartag.sock",
                    "HERDR_PLUGIN_STATE_DIR": state_dir,
                    "HERDR_TAB_ID": "w1:t1",
                }
                result = _run([command], env)
            self.assertEqual(result.returncode, 0, command)
            self.assertIn("Cannot connect to the Herdr socket", result.stdout + result.stderr, command)

if __name__ == "__main__":
    unittest.main()
