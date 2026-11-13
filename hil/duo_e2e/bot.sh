#!/bin/bash
# Coordinator only. Stops or starts the A/B duo bot on 127.0.0.1:7861.
#
#   hil/duo_e2e/bot.sh stop
#   hil/duo_e2e/bot.sh start <variant> <on|off> [VAR=value ...]
#
# start runs $DUO_E2E_LAUNCHERS/run-<variant>-bot.sh (default directory
# ~/src/evidence/duo/demo, where the variants are base and unify) as the
# transient user unit duo-ab-bot, with auto-switch on or off and every further
# VAR=value in its environment. Those land on a command line, so no secrets
# there: the launcher loads its own credentials. The unit's journal identifier
# is the launcher's name, run-<variant>-bot.sh: the tag journal.py is given.
#
# Both commands first free port 7861, and that is destructive on purpose:
#   - the user units duo-demo-bot (the standing demo bot) and duo-ab-bot are
#     stopped, with SIGKILL where they do not stop at once. The demo bot is
#     not started again afterwards: that is yours to do when you are done;
#   - a process that still listens on TCP 7861 half a minute later is killed
#     with SIGKILL if it is a duo bot of this repository. If it is anything
#     else, nothing more is done and the command fails: free the port yourself;
#   - every other duo bot of this repository is killed with SIGKILL, from
#     whichever worktree it runs. A bot that was told to stop keeps serving
#     the sessions it has, and those would answer the next run's device.
# "A duo bot of this repository" is decided per process by botproc.py: this
# user's, a Python interpreter, started exactly as the launchers start it,
#     <python> server/duo_bot.py -t webrtc --host 127.0.0.1 --port 7861
# from a checkout of this repository. So no other duo bot of yours that was
# started that way may be in use on this host: no demo. A bot started in any
# other way is never killed here: a HIL run's (hil/duo_identity.py serve), or
# one with an interpreter option. If it holds the port the command fails, and
# it is yours to stop.
#
# With anything but stop or start as the first argument, nothing is touched.
set -u
port=7861
here=$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)
usage() { echo "usage: bot.sh stop | bot.sh start <variant> <on|off> [VAR=value ...]" >&2; exit 2; }

stop() {
  systemctl --user stop duo-demo-bot 2>/dev/null &
  systemctl --user kill -s SIGKILL duo-ab-bot 2>/dev/null; systemctl --user stop duo-ab-bot 2>/dev/null
  sleep 1; systemctl --user kill -s SIGKILL duo-demo-bot 2>/dev/null; wait
  systemctl --user reset-failed duo-ab-bot duo-demo-bot 2>/dev/null
  for _ in $(seq 30); do ss -ltn | grep -q ":$port " || break; sleep 1; done
  pid=$(ss -ltnp | grep ":$port " | grep -oP 'pid=\K[0-9]+' | head -1)
  if [ -n "$pid" ]; then
    python3 "$here/botproc.py" check "$pid" ||
      { echo "bot.sh: port $port is held by pid $pid, which is not a launcher-started duo bot of this repository; not killed" >&2; exit 1; }
    kill -9 "$pid"
  fi
  for pid in $(python3 "$here/botproc.py" pids); do kill -9 "$pid"; done
  sleep 1; ss -ltn | grep -q ":$port " && { echo "$port still busy"; exit 1; }; echo stopped
}

case "${1:-}" in
  stop) [ $# -eq 1 ] || usage; stop; exit ;;
  start) [ $# -ge 3 ] || usage ;;
  *) usage ;;
esac
variant=$2; mode=$3; shift 3
[[ $variant =~ ^[A-Za-z0-9_-]+$ ]] || usage
case "$mode" in on) auto=1 ;; off) auto=0 ;; *) usage ;; esac
launcher=${DUO_E2E_LAUNCHERS:-$HOME/src/evidence/duo/demo}/run-$variant-bot.sh
# Checked before anything is stopped: a mistyped variant must not cost the running bot.
[ -x "$launcher" ] || { echo "bot.sh: no executable $launcher" >&2; exit 2; }
stop
envs=(SERVER_PORT=$port ECHOEAR_STT_PROFILE=demo PIPECAT_SCTP_MAX_CHUNK_SIZE=1100 LOGURU_COLORIZE=NO PYTHONUNBUFFERED=1
      OPENBLAS_NUM_THREADS=1 PYTHONDONTWRITEBYTECODE=1 DUO_TEST=0 DUO_AUTO_SWITCH_OVERRIDE=$auto "$@")
args=(); for setting in "${envs[@]}"; do args+=(--setenv="$setting"); done
systemd-run --user --unit=duo-ab-bot --collect -p WorkingDirectory="$HOME" "${args[@]}" "$launcher" >/dev/null
for _ in $(seq 60); do ss -ltn | grep -q ":$port " && { echo "started $variant $mode"; exit 0; }; sleep 1; done
# The launcher's own lines are not printed: nothing here redacts them. Read them yourself.
echo "start failed; see: journalctl --user -t run-$variant-bot.sh -n 20 -o cat" >&2; exit 1
