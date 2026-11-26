"""Tests for reading pane activity (foreground program, ssh target, remote title)."""
from __future__ import annotations

import unittest

from eartag.activity import command_argv, is_shell, leader_process, program_name, remote_location, ssh_target


class LeaderProcessTest(unittest.TestCase):
    def test_leader_process(self) -> None:
        info = {
            "foreground_process_group_id": 1,
            "foreground_processes": [{"pid": 3, "name": "node"}, {"pid": 1, "name": "zsh"}],
        }
        self.assertEqual(leader_process(info), {"pid": 1, "name": "zsh"})
        # Observed (top): a reply with no foreground_processes key at all
        self.assertIsNone(leader_process({"pane_id": "p", "foreground_process_group_id": 1}))
        self.assertIsNone(leader_process(None))


class ProgramNameTest(unittest.TestCase):
    def test_program_name(self) -> None:
        self.assertEqual(program_name({"argv0": "-zsh", "name": "zsh", "pid": 1}), "zsh")
        self.assertEqual(
            program_name({"argv": ["node", "/usr/local/bin/npm", "run", "dev"], "argv0": "node", "name": "node", "pid": 1}),
            "npm",
        )
        self.assertEqual(program_name({"argv0": "/usr/bin/vim", "name": "vim", "pid": 1}), "vim")
        self.assertEqual(program_name({"name": "top", "pid": 1}), "top")
        # Observed: macOS python3 re-executes itself as a 'Python' binary.
        framework = "/Library/Frameworks/Python3.framework/Versions/3.9/Resources/Python.app/Contents/MacOS/Python"
        self.assertEqual(program_name({"argv0": framework, "argv": [framework, "-c", "pass"], "name": "Python", "pid": 1}), "python")
        self.assertEqual(program_name({"argv0": "python3.9", "argv": ["python3.9", "app.py"], "name": "python3.9", "pid": 1}), "app.py")
        self.assertIsNone(program_name({"pid": 1}))

    def test_is_shell(self) -> None:
        self.assertTrue(is_shell("zsh"))
        self.assertFalse(is_shell("vim"))
        self.assertFalse(is_shell(None))


class SshTargetTest(unittest.TestCase):
    def test_ssh_target(self) -> None:
        self.assertEqual(ssh_target("ssh", ["ssh", "prod"]), "prod")
        self.assertEqual(ssh_target("ssh", ["ssh", "-p", "2222", "deploy@10.0.0.5"]), "10.0.0.5")
        self.assertEqual(ssh_target("ssh", ["ssh", "-i", "k", "-J", "bastion", "ssh://u@db:22"]), "db")
        self.assertEqual(ssh_target("ssh", ["ssh", "-tt", "-oBatchMode=yes", "prod", "ls"]), "prod")
        self.assertIsNone(ssh_target("ssh", ["ssh", "-V"]))
        self.assertEqual(ssh_target("mosh", ["mosh", "--ssh=ssh -p 22", "u@prod"]), "prod")
        self.assertEqual(ssh_target("mosh", ["mosh", "-p", "60001", "prod"]), "prod")
        # The -P tag option from recent OpenSSH
        self.assertEqual(ssh_target("ssh", ["ssh", "-P", "work", "prod"]), "prod")

    def test_mosh_as_perl_script(self) -> None:
        # mosh is a perl script, so its argv actually shows up as ["perl", ".../mosh", "prod"] (shebang execution).
        proc = {"pid": 1, "name": "perl", "argv0": "perl", "argv": ["perl", "/usr/local/bin/mosh", "prod"]}
        self.assertEqual(program_name(proc), "mosh")
        self.assertEqual(command_argv(proc), ["/usr/local/bin/mosh", "prod"])
        self.assertEqual(ssh_target("mosh", command_argv(proc)), "prod")

    def test_mosh_client(self) -> None:
        # After connecting, mosh execs into mosh-client. The original command line follows -#.
        argv = ["mosh-client", "-#", "prod | 192.0.2.1 60001", "192.0.2.1", "60001"]
        self.assertEqual(ssh_target("mosh-client", argv), "prod")
        self.assertIsNone(ssh_target("mosh-client", ["mosh-client", "192.0.2.1", "60001"]))

    def test_command_argv_without_interpreter(self) -> None:
        self.assertEqual(command_argv({"pid": 1, "argv": ["ssh", "prod"]}), ["ssh", "prod"])
        self.assertEqual(command_argv({"pid": 1, "name": "x"}), [])


class RemoteLocationTest(unittest.TestCase):
    def test_remote_location(self) -> None:
        self.assertEqual(remote_location("u@prod: ~/app", "mac"), ("prod", "app"))
        self.assertEqual(remote_location("u@prod:~", "mac"), ("prod", "~"))
        self.assertEqual(remote_location("prod:/srv/web/", "mac"), ("prod", "web"))
        # A leftover local shell title is not mistaken for a remote one.
        self.assertIsNone(remote_location("u@mac:/x", "mac"))
        self.assertIsNone(remote_location("vim a.py", "mac"))
        self.assertIsNone(remote_location("", "mac"))


if __name__ == "__main__":
    unittest.main()
