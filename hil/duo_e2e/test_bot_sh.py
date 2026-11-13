"""bot.sh with every service, listener, process and signal boundary replaced.

The script is sourced by a shell in which systemctl, systemd-run, ss, kill, sleep, journalctl,
pgrep and python3 are functions, and whose PATH holds seven harmless tools and nothing else. So
no real unit is touched, no port is looked at and no signal is sent, whatever the script does.
"""
import os
from pathlib import Path
import shutil
import subprocess
import sys

import botproc
from conftest import WAITS

SCRIPT = Path(__file__).with_name("bot.sh")
BOUNDARIES = r'''
systemctl() { echo "systemctl $*" >> "$LOG"; }
systemd-run() { echo "systemd-run $*" >> "$LOG"; [ -z "$COMES_UP" ] || echo 40404 > "$LISTENER"; }
ss() { local pid; pid=$(cat "$LISTENER"); [ -z "$pid" ] || echo "LISTEN 0 128 127.0.0.1:7861 0.0.0.0:* users:((\"x\",pid=$pid,fd=7))"; }
kill() { echo "kill $*" >> "$LOG"; [ "$2" != "$(cat "$LISTENER")" ] || : > "$LISTENER"; }
sleep() { :; }
journalctl() { echo "journalctl $*" >> "$LOG"; echo "Authorization: Bearer SECRET-MARKER https://secret.example/token"; }
pgrep() { echo "pgrep $*" >> "$LOG"; echo 99999; }
'''
# The verifier as the test dictates it ...
DICTATED = r'''
python3() {
  echo "python3 $(basename "$1") $2 ${3:-}" >> "$LOG"
  case "$2" in
    check) case " $VERIFIED " in *" $3 "*) return 0 ;; *) return 1 ;; esac ;;
    pids) echo "$STALE" ;;
  esac
}
'''
# ... or botproc.py itself, on the real /proc. Only for a bot.sh inside the sandbox fixture.
REAL = r'''
python3() { echo "python3 $(basename "$1") $2 ${3:-}" >> "$LOG"; "$PYTHON" "$@"; }
'''


def bot(tmp_path, *arguments, listener="", verified="", stale="", comes_up=False, launcher=True, script=SCRIPT,
        verifier=DICTATED):
    tools = tmp_path / "tools"
    if not tools.exists():
        tools.mkdir()
        for name in ("basename", "cat", "dirname", "grep", "head", "seq", "git"):  # git: botproc.py asks it.
            os.symlink(shutil.which(name), tools / name)
    launchers = tmp_path / "launchers"
    launchers.mkdir(exist_ok=True)
    if launcher:
        (launchers / "run-unify-bot.sh").write_text("#!/bin/sh\n")
        (launchers / "run-unify-bot.sh").chmod(0o755)
    log = tmp_path / "calls"
    log.write_text("")
    (tmp_path / "listener").write_text(listener)
    done = subprocess.run(["/bin/bash", "-c", BOUNDARIES + verifier + f'source "{script}" "$@"', "bot.sh", *arguments],
                          capture_output=True, text=True, timeout=60,
                          env=dict(PATH=str(tools), HOME=str(tmp_path), LOG=str(log), LISTENER=str(tmp_path / "listener"),
                                   VERIFIED=verified, STALE=stale, COMES_UP="1" if comes_up else "",
                                   DUO_E2E_LAUNCHERS=str(launchers), PYTHON=sys.executable))
    return done, log.read_text().splitlines()


def kills(calls):
    return [call for call in calls if call.startswith("kill ")]


def test_stop_stops_the_two_owned_units_and_kills_nothing_when_nothing_is_left(tmp_path):
    done, calls = bot(tmp_path, "stop")
    assert (done.returncode, done.stdout) == (0, "stopped\n")
    assert sorted(call for call in calls if call.startswith("systemctl")) == [
        "systemctl --user kill -s SIGKILL duo-ab-bot", "systemctl --user kill -s SIGKILL duo-demo-bot",
        "systemctl --user reset-failed duo-ab-bot duo-demo-bot", "systemctl --user stop duo-ab-bot",
        "systemctl --user stop duo-demo-bot"]
    assert kills(calls) == [] and "python3 botproc.py pids " in calls


def test_stop_refuses_a_listener_that_is_not_a_duo_bot_and_kills_nothing(tmp_path):
    done, calls = bot(tmp_path, "stop", listener="10101", stale="30303")
    assert done.returncode == 1 and "stopped" not in done.stdout
    assert done.stderr == ("bot.sh: port 7861 is held by pid 10101, which is not a launcher-started duo bot of this "
                           "repository; not killed\n")
    # Not the listener, and not the stale bots either: the run is abandoned as it stands.
    assert kills(calls) == [] and "python3 botproc.py check 10101" in calls and "python3 botproc.py pids " not in calls


def test_stop_kills_a_listener_that_is_a_verified_duo_bot(tmp_path):
    done, calls = bot(tmp_path, "stop", listener="20202", verified="20202")
    assert (done.returncode, done.stdout) == (0, "stopped\n")
    assert kills(calls) == ["kill -9 20202"]


def test_stop_kills_the_stale_bots_the_verifier_names_and_no_others(tmp_path):
    done, calls = bot(tmp_path, "stop", stale="30303 30304")
    assert (done.returncode, done.stdout) == (0, "stopped\n")
    assert kills(calls) == ["kill -9 30303", "kill -9 30304"]
    assert not any(call.startswith("pgrep") for call in calls)  # No pattern match over command lines any more.


def test_a_failed_start_prints_no_journal_line(tmp_path):
    done, calls = bot(tmp_path, "start", "unify", "on")
    assert done.returncode == 1
    assert done.stderr == "start failed; see: journalctl --user -t run-unify-bot.sh -n 20 -o cat\n"
    assert "SECRET-MARKER" not in done.stdout + done.stderr and "secret.example" not in done.stdout + done.stderr
    assert not any(call.startswith("journalctl") for call in calls)


def test_start_runs_the_launcher_as_the_transient_unit_with_the_flags_given(tmp_path):
    done, calls = bot(tmp_path, "start", "unify", "on", "DUO_STT_KEYTERMS=1", comes_up=True)
    assert (done.returncode, done.stdout) == (0, "stopped\nstarted unify on\n")
    [started] = [call for call in calls if call.startswith("systemd-run")]
    assert started.startswith("systemd-run --user --unit=duo-ab-bot --collect ")
    assert "--setenv=SERVER_PORT=7861" in started and "--setenv=DUO_AUTO_SWITCH_OVERRIDE=1" in started
    assert "--setenv=DUO_STT_KEYTERMS=1" in started and started.endswith(f"{tmp_path}/launchers/run-unify-bot.sh")
    off, calls = bot(tmp_path, "start", "unify", "off", comes_up=True)
    assert "--setenv=DUO_AUTO_SWITCH_OVERRIDE=0" in next(call for call in calls if call.startswith("systemd-run"))


def test_anything_but_stop_or_a_complete_start_touches_nothing(tmp_path):
    for arguments in ((), ("restart",), ("start",), ("start", "unify"), ("start", "unify", "maybe"),
                      ("start", "un/ify", "on"), ("stop", "now")):
        done, calls = bot(tmp_path, *arguments)
        assert done.returncode == 2 and calls == [], arguments
    # A variant with no launcher is refused before the running bot is stopped.
    done, calls = bot(tmp_path, "start", "base", "on")
    assert done.returncode == 2 and calls == [] and "no executable" in done.stderr


def test_the_port_freed_is_the_port_a_verified_bot_was_started_on():
    assert f"\nport={botproc.LAUNCHED[-1]}\n" in SCRIPT.read_text()


def test_with_the_real_verifier_only_a_bot_started_as_the_launchers_start_it_is_killed(tmp_path, sandbox):
    """Sol's K1-R through both paths of the real script: real processes, the real /proc, botproc.py unmocked."""
    script = sandbox.root / "hil/duo_e2e/bot.sh"
    run = botproc.LAUNCHED
    genuine = sandbox.start(*run)
    impostors = [sandbox.start("-c" + WAITS, *run), sandbox.start("-munrelated_module", *run),
                 sandbox.start("-uc" + WAITS, *run)]
    # The listener path: with any of the three on the port, the command fails and kills nothing at all.
    for impostor in impostors:
        done, calls = bot(tmp_path, "stop", listener=str(impostor), script=script, verifier=REAL)
        assert done.returncode == 1 and done.stderr.startswith(f"bot.sh: port 7861 is held by pid {impostor}, ")
        assert kills(calls) == [] and f"python3 botproc.py check {impostor}" in calls
    # The stale-process path: of the four, the one that was started as the launchers start it.
    done, calls = bot(tmp_path, "stop", script=script, verifier=REAL)
    assert (done.returncode, done.stdout) == (0, "stopped\n")
    assert kills(calls) == [f"kill -9 {genuine}"]
    # The listener path with that one on the port. (kill is a function here, so the stale path finds it again.)
    done, calls = bot(tmp_path, "stop", listener=str(genuine), script=script, verifier=REAL)
    assert (done.returncode, done.stdout) == (0, "stopped\n")
    assert kills(calls) == [f"kill -9 {genuine}"] * 2
