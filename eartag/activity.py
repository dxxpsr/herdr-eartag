"""Pure functions that work out "what a pane is doing right now".

Inputs are only the Herdr `pane.process_info` reply and the terminal title (no external calls).
- Leader of the foreground process group = the command the user ran in the shell (e.g. npm for `npm run dev`)
- Extract the connection target from an ssh/mosh command line
- Extract the remote location from a `user@host:path` title sent by a remote shell
"""
from __future__ import annotations

import os
import re
from typing import List, Optional, Tuple

# A shell as leader counts as "idle".
SHELLS = frozenset({"zsh", "bash", "sh", "fish", "dash", "ksh", "tcsh", "nu"})
# For scripts run by an interpreter, the script name is more meaningful (node .../npm → npm).
# python, python3, python3.9 and other version suffixes all count as interpreters.
INTERPRETER_RE = re.compile(r"^(node|python3?|ruby|perl)(?:[\d.]*)$")
# Remote connection commands. Show the connection target instead of the local folder.
# mosh execs into mosh-client after connecting, so recognize both.
REMOTE_PROGRAMS = frozenset({"ssh", "mosh", "mosh-client"})

# Single-letter ssh options that take a value (per man ssh, including -P tag from recent OpenSSH)
SSH_VALUE_OPTS = frozenset("BbcDEeFIiJLlmOoPpQRSWw")
# mosh options that take a value (long options may be `--x=value` or `--x value`)
MOSH_VALUE_SHORT = frozenset("p")
MOSH_VALUE_LONG = frozenset(
    {"--ssh", "--port", "--predict", "--family", "--server", "--client", "--bind-server", "--experimental-remote-ip"}
)

# Remote shell title forms: "user@host:path" or "host: path" (default Ubuntu bash, etc.)
REMOTE_TITLE_RE = re.compile(r"^(?:[^@\s]+@)?([^:\s]+):\s*(\S.*)$")


def leader_process(process_info: Optional[dict]) -> Optional[dict]:
    """The foreground process group leader (pid == group ID). None if the info is missing."""
    if not isinstance(process_info, dict):
        return None
    group_id = process_info.get("foreground_process_group_id")
    # Observed: while top is running, the foreground_processes key may be missing entirely.
    processes = process_info.get("foreground_processes") or []
    if group_id is None:
        return None
    for proc in processes:
        if isinstance(proc, dict) and proc.get("pid") == group_id:
            return proc
    return None


def _basename(value: str) -> str:
    # A login shell's argv0 starts with '-', like "-zsh".
    return os.path.basename(value.rstrip("/")).lstrip("-")


def program_name(proc: dict) -> Optional[str]:
    """A human-readable name for the process. For an interpreter, the name of the script it runs."""
    argv: List[str] = [a for a in (proc.get("argv") or []) if isinstance(a, str)]
    raw = proc.get("argv0") or (argv[0] if argv else None) or proc.get("name")
    if not raw or not isinstance(raw, str):
        return None
    name = _basename(raw)
    interpreter = _interpreter_name(name)
    if interpreter is None:
        return name or None
    if len(argv) > 1 and not argv[1].startswith("-"):
        script = _basename(argv[1])
        if script:
            return script
    # Run without a script (REPL, -c, etc.): show the normalized interpreter name.
    return interpreter


def _interpreter_name(name: str) -> Optional[str]:
    """The normalized name if it is an interpreter, otherwise None.

    macOS python3 re-executes itself as a 'Python' binary (observed),
    and versions like 'python3.9' appear too, so compare after lowercasing and stripping the version suffix.
    """
    match = INTERPRETER_RE.match(name.lower())
    if not match:
        return None
    base = match.group(1)
    return "python" if base.startswith("python") else base


def is_shell(program: Optional[str]) -> bool:
    return program in SHELLS


def command_argv(proc: dict) -> List[str]:
    """argv of the command the user ran. For a script run by an interpreter, drop the interpreter.

    Example: the shebang script mosh shows up as ["perl", "/usr/local/bin/mosh", "prod"],
    so return ["/usr/local/bin/mosh", "prod"] (so target extraction does not mistake the script path for the target).
    """
    argv = [a for a in (proc.get("argv") or []) if isinstance(a, str)]
    if len(argv) > 1 and _interpreter_name(_basename(argv[0])) and not argv[1].startswith("-"):
        return argv[1:]
    return argv


def remote_display_name(program: str) -> str:
    """Remote program name shown on the tab (mosh-client → mosh)."""
    return "mosh" if program == "mosh-client" else program


def _clean_host(target: str) -> Optional[str]:
    """`ssh://user@host:port` → `host`."""
    if target.startswith("ssh://"):
        target = target[len("ssh://"):]
    target = target.rsplit("@", 1)[-1]
    if target.startswith("["):  # IPv6 [::1]:22
        target = target[1:].split("]", 1)[0]
    elif target.count(":") == 1:
        target = target.split(":", 1)[0]
    target = target.strip("/")
    return target or None


def _first_ssh_operand(args: List[str]) -> Optional[str]:
    index = 0
    while index < len(args):
        arg = args[index]
        if arg == "--":
            return args[index + 1] if index + 1 < len(args) else None
        if arg.startswith("-") and len(arg) > 1:
            # Handles "-tt", "-p2222", "-oBatchMode=yes" and "-p 2222".
            for pos, char in enumerate(arg[1:], start=1):
                if char in SSH_VALUE_OPTS:
                    if pos == len(arg) - 1:
                        index += 1  # the value is in the next argument
                    break
            index += 1
            continue
        return arg
    return None


def _first_mosh_operand(args: List[str]) -> Optional[str]:
    index = 0
    while index < len(args):
        arg = args[index]
        if arg == "--":
            return args[index + 1] if index + 1 < len(args) else None
        if arg.startswith("--"):
            if "=" not in arg and arg in MOSH_VALUE_LONG:
                index += 1
            index += 1
            continue
        if arg.startswith("-") and len(arg) > 1:
            if arg[-1] in MOSH_VALUE_SHORT and len(arg) == 2:
                index += 1
            index += 1
            continue
        return arg
    return None


def _mosh_client_target(args: List[str]) -> Optional[str]:
    """mosh-client receives the original target as `-# "prod | IP port"`."""
    if "-#" not in args:
        return None
    index = args.index("-#")
    if index + 1 >= len(args):
        return None
    words = args[index + 1].split()
    return words[0] if words else None


def ssh_target(program: str, argv: List[str]) -> Optional[str]:
    """Extract the target host (aliases included) from an ssh/mosh command line. None if it cannot."""
    args = [a for a in argv[1:] if isinstance(a, str)]
    if program == "mosh-client":
        operand = _mosh_client_target(args)
    elif program == "mosh":
        operand = _first_mosh_operand(args)
    else:
        operand = _first_ssh_operand(args)
    return _clean_host(operand) if operand else None


def remote_location(title: str, local_host: str) -> Optional[Tuple[str, str]]:
    """(host, folder name) from a remote shell title. None if it is not a remote form or the host is local."""
    match = REMOTE_TITLE_RE.match((title or "").strip())
    if not match:
        return None
    host, path = match.group(1), match.group(2).strip()
    # Do not mistake a "user@my-mac:path" title left by the local shell right after ssh for a remote one.
    if local_host and host.split(".")[0].lower() == local_host.split(".")[0].lower():
        return None
    trimmed = path.rstrip("/")
    if trimmed in ("~", ""):
        return host, "~" if trimmed == "~" else "/"
    folder = os.path.basename(trimmed) or "/"
    return host, folder
