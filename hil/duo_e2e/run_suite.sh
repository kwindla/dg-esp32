#!/bin/bash
# Coordinator only. Plays cohorts through the web simulator, one run directory each.
#
#   DUO_E2E_NODE_MODULES=<dir> hil/duo_e2e/run_suite.sh <prefix> <cohort> [<cohort> ...]
#
# Needs the bot on 7861 (bot.sh start ...) and the simulator URL in
# ~/.config/duo-web.url. web_driver.mjs reads that file itself: the URL is
# never an argument and never echoed here.
#
#   DUO_E2E_COHORTS  where cohort.py wrote <cohort>.wav and <cohort>.json
#                    (default ~/src/evidence/duo/e2e/cohorts)
#   DUO_E2E_RUNS     where the run directories go (default ~/src/evidence/duo/e2e/runs)
#   DUO_E2E_TAPS     double-tap the face at these seconds after connect, such as 30,75,
#                    in every cohort of this invocation (default: no taps)
#
# Each run directory <prefix>-<cohort> gets start and end (the journal window),
# marks.json (the marks this run was played from), and the driver's web.log,
# web.png and played.json. Score it with journal.py.
set -u
[ $# -ge 2 ] || { echo "usage: run_suite.sh <prefix> <cohort> [<cohort> ...]" >&2; exit 2; }
here=$(cd "$(dirname "$0")" && pwd)
repo=$(cd "$here/../.." && pwd)
cohorts=${DUO_E2E_COHORTS:-$HOME/src/evidence/duo/e2e/cohorts}
runs=$(realpath -m "${DUO_E2E_RUNS:-$HOME/src/evidence/duo/e2e/runs}")
case "$runs/" in
  "$repo"/*) echo "run_suite: $runs is inside the repository; runs belong under ~/src/evidence" >&2; exit 2 ;;
esac
prefix=$1; shift
for cohort in "$@"; do
  [ -f "$cohorts/$cohort.wav" ] && [ -f "$cohorts/$cohort.json" ] ||
    { echo "run_suite: no $cohort.wav and $cohort.json in $cohorts; build them with cohort.py" >&2; exit 2; }
done
failed=0
for cohort in "$@"; do
  out=$runs/$prefix-$cohort
  mkdir -p "$out" || exit 2
  # Nothing of an earlier run with this name may pass for this one's.
  rm -f "$out/played.json" "$out/web.log" "$out/web.png" "$out/report.json" "$out/end"
  cp "$cohorts/$cohort.json" "$out/marks.json" || exit 2
  seconds=$(python3 -c 'import json, sys; print(json.load(open(sys.argv[1]))["run_s"])' "$out/marks.json") || exit 2
  date '+%Y-%m-%d %H:%M:%S' > "$out/start"
  timeout $((seconds + 60)) node "$here/web_driver.mjs" "$cohorts/$cohort.wav" "$seconds" "$out" \
    ${DUO_E2E_TAPS:+"$DUO_E2E_TAPS"}
  status=$?
  # A second later: journalctl --until stops at the start of the second it is given.
  date -d '1 second' '+%Y-%m-%d %H:%M:%S' > "$out/end"
  [ $status -eq 0 ] || failed=1
  echo "$cohort: driver exit $status, $out"
  sleep 3  # The bot ends the session before the next one starts.
done
echo "SUITE DONE $prefix"
exit $failed
