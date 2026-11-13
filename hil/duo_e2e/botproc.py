#!/usr/bin/env python3
"""Which processes bot.sh may kill: duo bots of this repository, and nothing else.

    botproc.py pids          the verified duo bots of this user, one PID per line
    botproc.py check <pid>   exit 0 if that process is one of them, 1 if it is not

A process is a duo bot of this repository when all of this holds:

  - it belongs to the calling user;
  - its executable is a Python interpreter: /proc/<pid>/exe points at a file
    named python, python3 or python3.<minor>;
  - its arguments are exactly the ones the launchers give it,
        server/duo_bot.py -t webrtc --host 127.0.0.1 --port 7861
    with nothing before the script and nothing after the port;
  - server/duo_bot.py, taken from the process's own working directory, is a
    file in a checkout of this repository: a worktree whose git directory is
    this one's.

Anything else is not killed: a command line that merely contains the words, an
editor with the file open, a bot of another project, a wrapper such as `uv run`
(it ends with its child), and any Python started with an interpreter option.
The last matters. `python -cpass server/duo_bot.py` runs the code after -c, and
the file name is only an argument to it; so does -ucpass, and -mmodule runs a
module. No option is parsed here: a command line that has one is refused, and
so is a duo bot of this repository that was started in any other way. Whoever
started that one stops it.
"""
import os
from pathlib import Path
import re
import subprocess
import sys

ROOT = Path(__file__).resolve().parents[2]
# Everything after the interpreter, as run-<variant>-bot.sh starts the bot. bot.sh frees the same port.
LAUNCHED = ("server/duo_bot.py", "-t", "webrtc", "--host", "127.0.0.1", "--port", "7861")
INTERPRETER = re.compile(r"python(3(\.\d+)?)?")


def common_dir(checkout):
    """The git directory a checkout's history lives in: the same for every worktree of one repository."""
    try:
        found = subprocess.run(["git", "-C", str(checkout), "rev-parse", "--path-format=absolute", "--git-common-dir"],
                               capture_output=True, text=True, timeout=10)
    except (OSError, subprocess.TimeoutExpired):
        return None
    return Path(found.stdout.strip()).resolve() if found.returncode == 0 and found.stdout.strip() else None


def duo_bot(pid, *, proc=Path("/proc"), uid=None, repository=None, common=common_dir):
    """The resolved server/duo_bot.py that process `pid` runs, if it is a duo bot of this repository; else None."""
    entry = proc / str(pid)
    try:
        if entry.stat().st_uid != (os.getuid() if uid is None else uid):
            return None
        if not INTERPRETER.fullmatch(Path(os.readlink(entry / "exe")).name):
            return None
        # One NUL ends each argument, the last one too. Compared as bytes, whole and in order.
        command = (entry / "cmdline").read_bytes()
        if not command.endswith(b"\0") or command[:-1].split(b"\0")[1:] != [part.encode() for part in LAUNCHED]:
            return None
        script = (Path(os.readlink(entry / "cwd")) / LAUNCHED[0]).resolve()
    except (OSError, ValueError):
        return None  # The process is gone, or is not ours to look at.
    if script.name != "duo_bot.py" or script.parent.name != "server" or not script.is_file():
        return None
    ours = common(ROOT) if repository is None else repository
    return script if ours is not None and common(script.parent.parent) == ours else None


def pids(*, proc=Path("/proc"), **how):
    """Every verified duo bot, this process excepted."""
    found = []
    for entry in proc.iterdir():
        if entry.name.isdigit() and int(entry.name) != os.getpid() and duo_bot(int(entry.name), proc=proc, **how):
            found.append(int(entry.name))
    return sorted(found)


def main(argv=None):
    argv = sys.argv[1:] if argv is None else argv
    if argv == ["pids"]:
        print("\n".join(map(str, pids())))
        return 0
    if len(argv) == 2 and argv[0] == "check" and argv[1].isdigit():
        return 0 if duo_bot(int(argv[1])) else 1
    print("usage: botproc.py pids | botproc.py check <pid>", file=sys.stderr)
    return 2


if __name__ == "__main__":
    sys.exit(main())
