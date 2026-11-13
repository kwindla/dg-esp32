"""Which processes bot.sh may kill.

Most of it on a made-up /proc, where no real process is looked at. The last tests look at real ones: this
test's own children, started in a repository of their own. No test signals a process it did not start.
"""
import os
from pathlib import Path
import types

import pytest

import botproc
from conftest import WAITS

RUN = list(botproc.LAUNCHED)   # server/duo_bot.py -t webrtc --host 127.0.0.1 --port 7861
TAIL = RUN[1:]


def process(proc, pid, argv, cwd, exe="python3.12"):
    entry = proc / str(pid)
    entry.mkdir(parents=True)
    (entry / "cmdline").write_bytes(b"\0".join(part.encode() for part in argv) + b"\0")
    os.symlink(cwd, entry / "cwd")
    binary = proc.parent / "bin" / exe
    binary.parent.mkdir(exist_ok=True)
    binary.touch()
    os.symlink(binary, entry / "exe")
    return pid


@pytest.fixture
def world(tmp_path):
    """Three checkouts with a server/duo_bot.py each: two worktrees of this repository, one of another project."""
    ours, worktree, theirs = tmp_path / "ours", tmp_path / "worktree", tmp_path / "theirs"
    for checkout in (ours, worktree, theirs):
        (checkout / "server").mkdir(parents=True)
        (checkout / "server/duo_bot.py").write_text("")
    (ours / "server/duo_bot.py.backup").write_text("")
    (ours / "hil").mkdir()
    (ours / "hil/duo_identity.py").write_text("")
    (ours / "other.py").write_text("")
    proc = tmp_path / "proc"
    proc.mkdir()
    how = dict(proc=proc, repository="ours.git",
               common=lambda checkout: "ours.git" if Path(checkout) in (ours, worktree) else "theirs.git")
    return types.SimpleNamespace(ours=ours, worktree=worktree, theirs=theirs, nowhere=tmp_path, server=ours / "server",
                                 proc=proc, how=how)


def test_a_python_started_as_the_launchers_start_it_is_one(world):
    script = world.ours / "server/duo_bot.py"
    # Whatever the interpreter was called on the command line: its executable is what counts.
    for pid, (called, exe) in enumerate([("python", "python3.12"), ("/venv/bin/python", "python3"),
                                         ("python3", "python"), ("-anything", "python3.13")], 1):
        assert botproc.duo_bot(process(world.proc, pid, [called, *RUN], world.ours, exe), **world.how) == script
    # From whichever worktree of this repository it runs.
    assert (botproc.duo_bot(process(world.proc, 9, ["python", *RUN], world.worktree), **world.how)
            == world.worktree / "server/duo_bot.py")


@pytest.mark.parametrize("argv,cwd,exe", [
    # Sol's K1-R. With an attached or bundled -c or -m the interpreter runs that code or module, and the
    # script's name is one of its arguments: bare, and with everything after it as the launchers write it.
    (["python", "-cpass", "server/duo_bot.py"], "ours", "python3.12"),
    (["python", "-munrelated_module", "server/duo_bot.py"], "ours", "python3.12"),
    (["python", "-ucpass", "server/duo_bot.py"], "ours", "python3.12"),
    (["python", "-cpass", *RUN], "ours", "python3.12"),
    (["python", "-munrelated_module", *RUN], "ours", "python3.12"),
    (["python", "-ucpass", *RUN], "ours", "python3.12"),
    # No interpreter option is parsed. The separated forms, and options that do leave the script the
    # program, are refused alike.
    (["python", "-c", "pass", *RUN], "ours", "python3.12"),
    (["python", "-m", "pytest", *RUN], "ours", "python3.12"),
    (["python", "-", *RUN], "ours", "python3.12"),
    (["python", "--", *RUN], "ours", "python3.12"),
    (["python", "-u", *RUN], "ours", "python3.12"),
    (["python", "-I", *RUN], "ours", "python3.12"),
    (["python", "-X", "dev", "-W", "error", *RUN], "ours", "python3.12"),
    # The launchers' arguments, whole and in order: nothing missing, added, changed or respelled.
    (["python", "server/duo_bot.py"], "ours", "python3.12"),
    (["python", *RUN[:-1]], "ours", "python3.12"),
    (["python", *RUN, "-v"], "ours", "python3.12"),
    (["python", *RUN, ""], "ours", "python3.12"),
    (["python", *RUN[:-1], "7860"], "ours", "python3.12"),
    (["python", *RUN[:4], "0.0.0.0", *RUN[5:]], "ours", "python3.12"),
    (["python", "server/duo_bot.py", "--host", "127.0.0.1", "-t", "webrtc", "--port", "7861"], "ours", "python3.12"),
    (["python", "./server/duo_bot.py", *TAIL], "ours", "python3.12"),
    (["python", "{ours}/server/duo_bot.py", *TAIL], "ours", "python3.12"),
    (["python", "duo_bot.py", *TAIL], "server", "python3.12"),
    (["python", "server/duo_botXpy", *TAIL], "ours", "python3.12"),        # The old pattern's dot matched any character.
    (["python", "server/duo_bot.py.backup", *TAIL], "ours", "python3.12"), # ... and any suffix.
    (["python", "other.py", *RUN], "ours", "python3.12"),                  # The name is an argument, not the program.
    (["python", "hil/duo_identity.py", "serve", "out", *RUN], "ours", "python3.12"),
    (["python"], "ours", "python3.12"),
    # The launchers' command line, but not a bot of this repository.
    (["python", *RUN], "theirs", "python3.12"),                            # Another project's bot.
    (["python", *RUN], "nowhere", "python3.12"),                           # No such file from that directory.
    # ... or not a Python interpreter.
    (["bash", "-c", "python " + " ".join(RUN)], "ours", "bash"),           # The words inside another command line.
    (["vim", *RUN], "ours", "vim"),                                        # An editor with the file open.
    (["uv", "run", "--project", "server", "python", *RUN], "ours", "uv"),  # The wrapper ends with its child.
    (["python", *RUN], "ours", "python3-config"),
    (["python", *RUN], "ours", "pythonista"),
    (["python", *RUN], "ours", "python2.7"),
    (["python", *RUN], "ours", "python3.12 (deleted)"),                    # Replaced on disk since: not identified.
])
def test_anything_else_is_not(world, argv, cwd, exe):
    argv = [part.format(ours=world.ours) for part in argv]
    pid = process(world.proc, 7, argv, getattr(world, cwd), exe)
    assert botproc.duo_bot(pid, **world.how) is None
    assert botproc.pids(**world.how) == []   # The stale-process path asks the same question.


@pytest.mark.parametrize("command", [
    b"",                                                                   # A zombie or a kernel thread.
    b"python\0" + b"\0".join(part.encode() for part in RUN),               # No NUL ends the last argument.
    b"python\0" + b"\0".join(part.encode() for part in RUN) + b"\0\0",
    b"python\0\xff\xfe\0" + b"\0".join(part.encode() for part in RUN) + b"\0",
    b"python " + " ".join(RUN).encode() + b"\0",                           # One argument that reads like eight.
])
def test_a_command_line_that_is_not_the_launchers_byte_for_byte_is_not(world, command):
    pid = process(world.proc, 8, ["python", *RUN], world.ours)
    assert botproc.duo_bot(pid, **world.how)
    (world.proc / "8/cmdline").write_bytes(command)
    assert botproc.duo_bot(pid, **world.how) is None


def test_another_users_process_a_vanished_one_and_an_unknown_repository_are_not(world):
    pid = process(world.proc, 5, ["python", *RUN], world.ours)
    assert botproc.duo_bot(pid, **world.how) and botproc.duo_bot(pid, **dict(world.how, uid=os.getuid() + 1)) is None
    assert botproc.duo_bot(999, **world.how) is None                        # No such process.
    (world.proc / "6").mkdir()                                              # It ended while it was being read.
    assert botproc.duo_bot(6, **world.how) is None
    assert botproc.duo_bot(pid, **dict(world.how, repository=None, common=lambda checkout: None)) is None  # git says nothing.


def test_pids_lists_the_verified_ones_only_and_never_this_process(world):
    process(world.proc, 11, ["python", *RUN], world.ours)
    process(world.proc, 12, ["python", *RUN], world.theirs)
    process(world.proc, 13, ["bash", "-c", "python " + " ".join(RUN)], world.ours, "bash")
    process(world.proc, 14, ["python", *RUN], world.worktree)
    process(world.proc, 15, ["python", "-ucpass", *RUN], world.ours)
    process(world.proc, 16, ["python", "-u", *RUN], world.ours)
    process(world.proc, os.getpid(), ["python", *RUN], world.ours)
    (world.proc / "self").mkdir()
    assert botproc.pids(**world.how) == [11, 14]


def test_the_command_line(world, monkeypatch, capsys):
    process(world.proc, 21, ["python", *RUN], world.ours)
    process(world.proc, 22, ["python", "-cpass", *RUN], world.ours)
    # main() takes no /proc of its own: its two questions are answered on the made-up one.
    monkeypatch.setattr(botproc, "duo_bot", lambda pid, real=botproc.duo_bot, **defaults: real(pid, **world.how))
    monkeypatch.setattr(botproc, "pids", lambda real=botproc.pids: real(**world.how))
    assert botproc.main(["check", "21"]) == 0 and botproc.main(["check", "22"]) == 1 and botproc.main(["check", "23"]) == 1
    assert botproc.main(["pids"]) == 0 and capsys.readouterr().out == "21\n"
    assert botproc.main([]) == 2 and botproc.main(["check", "x"]) == 2 and botproc.main(["kill", "21"]) == 2


def test_this_test_process_is_not_a_duo_bot():
    assert botproc.duo_bot(os.getpid()) is None


def test_real_processes_are_told_apart_by_how_they_were_started(sandbox):
    """The real /proc and the real git, through the sandbox's copy of the verifier with nothing injected."""
    genuine = sandbox.start(*RUN)
    impostors = [sandbox.start("-c" + WAITS, *RUN), sandbox.start("-munrelated_module", *RUN),
                 sandbox.start("-uc" + WAITS, *RUN), sandbox.start("-u", *RUN)]
    assert sandbox.botproc.duo_bot(genuine) == sandbox.root / "server/duo_bot.py"
    assert sandbox.botproc.main(["check", str(genuine)]) == 0
    for impostor in impostors:
        assert sandbox.botproc.duo_bot(impostor) is None
        assert sandbox.botproc.main(["check", str(impostor)]) == 1
    assert sandbox.botproc.pids() == [genuine]
    # To the verifier of this repository, the sandbox's bot is another project's.
    assert botproc.duo_bot(genuine) is None and genuine not in botproc.pids()
