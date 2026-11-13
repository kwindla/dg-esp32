#!/usr/bin/env bash
set -euo pipefail
repo_root=$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)
wrapper="$repo_root/scripts/build_locked.sh"
export TMPDIR="${TMPDIR:-$HOME/.cache/gap3-tmp}"
mkdir -p "$TMPDIR"
scratch=$(mktemp -d "$TMPDIR/build-lock-test.XXXXXX")
export DG_BUILD_LOCK_FILE="$scratch/build.lock"
pids=()
cleanup() {
    for pid in "${pids[@]}"; do kill "$pid" 2>/dev/null || true; done
    for pid in "${pids[@]}"; do wait "$pid" 2>/dev/null || true; done
    rm -rf "$scratch"
}
trap cleanup EXIT
unset DG_BUILD_LOCK_HELD DG_SERIAL_LOCK_HELD DG_BENCH_LOCK_FD DG_BOARD DG_DEMO_AUTOSTART DG_AEC

fail() { echo "FAIL: $*" >&2; exit 1; }
wait_marker() {
    for ((i=0; i<1000; i++)); do
        [[ -f "$1" ]] && return 0
        sleep 0.01
    done
    fail "timed out waiting for $1"
}
for code in 0 3 127; do
    result=0
    timeout 10 "$wrapper" bash -c 'exit "$1"' _ "$code" || result=$?
    [[ "$result" == "$code" ]] || fail "exit status $code became $result"
done
result=0
timeout 10 "$wrapper" dg-build-lock-test-missing-command >"$scratch/missing.log" 2>&1 || result=$?
[[ "$result" == 127 ]] || fail "missing command did not return 127"
timeout 10 "$wrapper" bash -c '
    [[ $# == 5 && "$1" == "two words" && -z "$2" && "$3" == "*" && "$4" == "\$literal;" && "$5" == "line
break" ]]
' _ 'two words' '' '*' '$literal;' $'line\nbreak' || fail "argument quoting"
timeout 10 "$wrapper" bash -c '
    [[ "$DG_BUILD_LOCK_HELD" == 1 ]] || exit 1
    timeout 5 "$1" bash -c '\''[[ "$DG_BUILD_LOCK_HELD" == 1 && "$1" == "nested argument" ]]'\'' _ "nested argument"
' _ "$wrapper" || fail "reentrancy"
DG_BUILD_LOCK_HELD=1 timeout 10 "$wrapper" bash -c '
    [[ "$DG_BUILD_LOCK_HELD" == 1 ]] || exit 1
    if flock -n "$DG_BUILD_LOCK_FILE" true; then exit 1; fi
' || fail "stale flag left the lock free"

# Real flock errors must fail immediately instead of entering the waiting loop.
mkdir -p "$scratch/flock-error"
printf '#!/usr/bin/env bash\nexit 64\n' > "$scratch/flock-error/flock"
chmod +x "$scratch/flock-error/flock"
for held in 0 1; do
    result=0
    PATH="$scratch/flock-error:$PATH" DG_BUILD_LOCK_HELD="$held" \
        timeout 10 "$wrapper" true >"$scratch/flock-error.log" 2>&1 || result=$?
    [[ "$result" == 64 ]] || fail "flock error became $result"
    grep -q 'flock exit 64' "$scratch/flock-error.log" || fail "unclear flock error"
    if grep -q 'waiting for build lock' "$scratch/flock-error.log"; then fail "flock error retried"; fi
done

# Hold the first critical section open until a second caller is waiting.
timeout 15 "$wrapper" bash -c '
    mkdir "$1/active" || exit 1
    date +%s%N > "$1/first-start"
    touch "$1/ready"
    while [[ ! -f "$1/release" ]]; do sleep 0.01; done
    date +%s%N > "$1/first-end"
    rmdir "$1/active"
' _ "$scratch" &
pids+=("$!")
wait_marker "$scratch/ready"
(
    touch "$scratch/second-attempt"
    exec timeout 15 "$wrapper" bash -c '
        mkdir "$1/active" || exit 1
        date +%s%N > "$1/second-start"
        rmdir "$1/active"
    ' _ "$scratch"
) &
pids+=("$!")
wait_marker "$scratch/second-attempt"
sleep 0.2
[[ ! -f "$scratch/second-start" ]] || fail "second holder entered before release"
touch "$scratch/release"
for pid in "${pids[@]}"; do wait "$pid" || fail "concurrent holder failed"; done
pids=()
[[ $(cat "$scratch/first-start") -le $(cat "$scratch/first-end") &&
   $(cat "$scratch/first-end") -le $(cat "$scratch/second-start") ]] || fail "holders overlapped"

# A fake IDF installation keeps routing tests entirely away from hardware.
mkdir -p "$scratch/bin"
cat > "$scratch/idf-env.bash" <<'SH'
function . {
    if [[ "$1" == "$HOME/esp/esp-idf-v6.1/export.sh" ]]; then
        [[ $(command -v python) == "$IDF_TEST_BIN/python" && $(command -v idf.py) == "$IDF_TEST_BIN/idf.py" ]]
        return
    fi
    builtin . "$@"
}
SH
cat > "$scratch/bin/idf.py" <<'SH'
#!/usr/bin/env bash
set -euo pipefail
if [[ "$EXPECT_BUILD_LOCK" == 1 ]]; then
    [[ "${DG_BUILD_LOCK_HELD:-}" == 1 ]]
    if flock -n "$DG_BUILD_LOCK_FILE" true; then exit 90; fi
else
    [[ "${DG_BUILD_LOCK_HELD:-}" != 1 ]]
    flock -n "$DG_BUILD_LOCK_FILE" true
fi
printf '%s\n' idf.py "$@" >> "$ARGV_LOG"
SH
cat > "$scratch/bin/python" <<'SH'
#!/usr/bin/env bash
set -euo pipefail
[[ "${DG_BUILD_LOCK_HELD:-}" != 1 ]]
flock -n "$DG_BUILD_LOCK_FILE" true
printf '%s\n' python "$@" >> "$ARGV_LOG"
if [[ "$1" == */board_port.py || "$1" == */boards.py ]]; then printf 'fake-board-port\n'; fi
SH
chmod +x "$scratch/bin/idf.py" "$scratch/bin/python"
export BASH_ENV="$scratch/idf-env.bash" PATH="$scratch/bin:$PATH" ARGV_LOG="$scratch/argv"
export IDF_TEST_BIN="$scratch/bin"
hash -r
[[ $(command -v python) == "$scratch/bin/python" && $(command -v idf.py) == "$scratch/bin/idf.py" ]] || fail "routing tools did not resolve to stubs"
for action in build reconfigure fullclean menuconfig set-target merge-bin secure-encrypt-flash-data efuse-common-table efuse-custom-table; do
    EXPECT_BUILD_LOCK=1 timeout 10 "$repo_root/scripts/idf.sh" "$action" 2>"$scratch/idf.stderr"
    grep -qx "$action" "$ARGV_LOG" || fail "lost build action $action"
    if [[ "$action" == menuconfig ]]; then
        grep -qx 'menuconfig holds the build lock until it exits' "$scratch/idf.stderr" || fail "missing menuconfig note"
    fi
done
# Pin the complete legacy EchoEar argv, not just selected flags.
local_defaults=""
if [[ -f sdkconfig.local ]]; then local_defaults=';sdkconfig.local'
elif [[ -f "$repo_root/sdkconfig.local" ]]; then local_defaults=";$repo_root/sdkconfig.local"; fi
legacy_defaults="sdkconfig.defaults$local_defaults;sdkconfig.defaults.echoear"
assert_argv() {
    printf '%s\n' "$@" > "$scratch/expected-argv"
    cmp -s "$scratch/expected-argv" "$ARGV_LOG" || fail "complete IDF argv mismatch"
}
for demo in 0 1; do
    rm -f "$ARGV_LOG"
    EXPECT_BUILD_LOCK=1 DG_BOARD=echoear DG_DEMO_AUTOSTART="$demo" timeout 10 "$repo_root/scripts/idf.sh" build
    expected_defaults="$legacy_defaults"
    expected_config="$repo_root/sdkconfig"
    if [[ "$demo" == 1 ]]; then
        expected_defaults+=';sdkconfig.defaults.demo';expected_config="$repo_root/build/sdkconfig-demo"
    fi
    assert_argv idf.py "-DSDKCONFIG_DEFAULTS=$expected_defaults" "-DSDKCONFIG=$expected_config" build
done
rm -f "$ARGV_LOG"
EXPECT_BUILD_LOCK=1 timeout 10 "$repo_root/scripts/idf.sh" -B build-explicit build
assert_argv idf.py "-DSDKCONFIG_DEFAULTS=$legacy_defaults" "-DSDKCONFIG=$repo_root/sdkconfig" -B build-explicit build
rm -f "$ARGV_LOG"
EXPECT_BUILD_LOCK=1 timeout 10 "$repo_root/scripts/idf.sh" "-DSDKCONFIG=$scratch/sdkconfig-explicit" reconfigure
assert_argv idf.py "-DSDKCONFIG_DEFAULTS=$legacy_defaults" "-DSDKCONFIG=$scratch/sdkconfig-explicit" reconfigure
# Main-project config chains have private build/config/lock paths. No real IDF
# process runs here: both executable resolutions are asserted above.
for aec in 0 1; do
    for demo in 0 1; do
        rm -f "$ARGV_LOG"
        EXPECT_BUILD_LOCK=1 DG_BOARD=p4xc DG_AEC="$aec" DG_DEMO_AUTOSTART="$demo" \
            timeout 10 "$repo_root/scripts/idf.sh" reconfigure
        suffix=""
        [[ "$aec" == 0 ]] || suffix+='-aec'
        [[ "$demo" == 0 ]] || suffix+='-demo'
        expected_defaults="$repo_root/boards/common/sdkconfig.defaults$local_defaults;$repo_root/boards/p4xc/sdkconfig.defaults"
        if [[ "$aec" == 1 ]]; then
            expected_defaults+=";$repo_root/boards/p4xc/sdkconfig.defaults.aec"
        fi
        if [[ "$demo" == 1 ]]; then
            expected_defaults+=';sdkconfig.defaults.demo'
        fi
        assert_argv idf.py -B "$repo_root/build-p4xc$suffix" -DIDF_TARGET=esp32p4 \
            "-DDEPENDENCIES_LOCK=$repo_root/dependencies.p4xc.lock" \
            "-DSDKCONFIG_DEFAULTS=$expected_defaults" "-DSDKCONFIG=$repo_root/build-p4xc$suffix/sdkconfig" reconfigure
    done
done
rm -f "$ARGV_LOG"
EXPECT_BUILD_LOCK=1 DG_BOARD=waveshare_146 timeout 10 "$repo_root/scripts/idf.sh" build
grep -qx "$repo_root/build-ws146" "$ARGV_LOG" || fail "Waveshare build path"
grep -qx -- "-DSDKCONFIG=$repo_root/build-ws146/sdkconfig" "$ARGV_LOG" || fail "Waveshare config path"
grep -qx -- "-DDEPENDENCIES_LOCK=$repo_root/dependencies.waveshare146.lock" "$ARGV_LOG" || fail "Waveshare lock path"
# Refuse every project-dir spelling for non-port board actions before either
# IDF or bench can run. These paths are private fixtures, not real spikes.
mkdir -p "$scratch/other"
for board in p4xc waveshare_146; do
    for action in build reconfigure menuconfig; do
        for form in short long short-attached long-attached; do
            case "$form" in
                short) project_args=(-C "$scratch/other") ;;
                long) project_args=(--project-dir "$scratch/other") ;;
                short-attached) project_args=("-C$scratch/other") ;;
                long-attached) project_args=("--project-dir=$scratch/other") ;;
            esac
            rm -f "$ARGV_LOG"
            result=0
            EXPECT_BUILD_LOCK=1 DG_BOARD="$board" timeout 10 "$repo_root/scripts/idf.sh" \
                "${project_args[@]}" "$action" > "$scratch/external-project.log" 2>&1 || result=$?
            [[ "$result" == 1 && ! -e "$ARGV_LOG" ]] || fail "$board external $action reached an exec"
            grep -q 'non-port actions require the main project' "$scratch/external-project.log" || fail "unclear external-project refusal"
        done
    done
    rm -f "$ARGV_LOG"
    EXPECT_BUILD_LOCK=1 DG_BOARD="$board" timeout 10 "$repo_root/scripts/idf.sh" -C "$repo_root" reconfigure
    grep -qx idf.py "$ARGV_LOG" || fail "explicit main project was refused"
    rm -f "$ARGV_LOG"
    result=0
    (cd "$scratch/other"; EXPECT_BUILD_LOCK=1 DG_BOARD="$board" timeout 10 "$repo_root/scripts/idf.sh" build) \
        > "$scratch/external-project.log" 2>&1 || result=$?
    [[ "$result" == 1 && ! -e "$ARGV_LOG" ]] || fail "$board external cwd reached IDF"
done
# Option values that happen to spell port actions are never actions themselves.
for option in -B --build-dir -C --project-dir -D --define-cache-entry -p --port --log-file; do
    rm -f "$ARGV_LOG"
    EXPECT_BUILD_LOCK=1 timeout 10 "$repo_root/scripts/idf.sh" "$option" build-flash build
    grep -qx idf.py "$ARGV_LOG" || fail "$option value was classified as a hardware action"
done
for option in '-Bbuild-flash' '--build-dir=build-flash' '-DSDKCONFIG=build-flash' '--port=build-flash'; do
    rm -f "$ARGV_LOG"
    EXPECT_BUILD_LOCK=1 timeout 10 "$repo_root/scripts/idf.sh" "$option" build
    grep -qx idf.py "$ARGV_LOG" || fail "$option was classified as a hardware action"
done
# IDF 6.1 -f (diag --force) and -t (--trust-flash-content) are flags.
# Conservatively detect a following port action instead of swallowing it.
for flag in -f -t; do
    rm -f "$ARGV_LOG"
    EXPECT_BUILD_LOCK=0 timeout 10 "$repo_root/scripts/idf.sh" diag "$flag" flash
    grep -q '/hil/bench.py$' "$ARGV_LOG" || fail "diag $flag flash skipped bench wrapper"
done
for action in flash monitor erase-flash erase_flash app-flash bootloader-flash partition-table-flash \
              encrypted-flash encrypted-app-flash erase-otadata read-otadata efuse-summary \
              efuse-burn-key dfu-flash coredump-info coredump-debug gdb gdbtui gdbgui openocd; do
    for bench_held in 0 1; do
        rm -f "$ARGV_LOG"
        result=0
        DG_BUILD_LOCK_HELD=1 DG_SERIAL_LOCK_HELD="$bench_held" \
            timeout 10 "$repo_root/scripts/idf.sh" "$action" >"$scratch/refusal.log" 2>&1 || result=$?
        [[ "$result" == 1 && ! -e "$ARGV_LOG" ]] || fail "locked $action reached an exec or timed out"
        grep -q 'DG_BUILD_LOCK_HELD=1' "$scratch/refusal.log" || fail "unclear refusal for $action"
    done
    rm -f "$ARGV_LOG"
    EXPECT_BUILD_LOCK=0 timeout 10 "$repo_root/scripts/idf.sh" "$action"
    grep -q '/hil/bench.py$' "$ARGV_LOG" || fail "$action skipped bench wrapper"
    rm -f "$ARGV_LOG"
    EXPECT_BUILD_LOCK=0 DG_SERIAL_LOCK_HELD=1 timeout 10 "$repo_root/scripts/idf.sh" "$action"
    grep -qx idf.py "$ARGV_LOG" || fail "$action did not reach stub IDF"
done
for action in flash monitor erase-flash efuse-summary; do
    rm -f "$ARGV_LOG"
    result=0
    EXPECT_BUILD_LOCK=0 DG_BOARD=waveshare_146 timeout 10 "$repo_root/scripts/idf.sh" "$action" >"$scratch/build-only.log" 2>&1 || result=$?
    [[ "$result" == 1 && ! -e "$ARGV_LOG" ]] || fail "build-only board reached hardware"
    grep -q 'build-only board' "$scratch/build-only.log" || fail "unclear build-only refusal"
done
for flag in DG_DEMO_AUTOSTART DG_AEC; do
    rm -f "$ARGV_LOG"
    result=0
    env DG_BOARD=waveshare_146 "$flag=1" timeout 10 "$repo_root/scripts/idf.sh" build >"$scratch/unsupported-variant.log" 2>&1 || result=$?
    [[ "$result" == 1 && ! -e "$ARGV_LOG" ]] || fail "unsupported Waveshare variant reached a build"
done
for port_option in -p --port; do
    rm -f "$ARGV_LOG"
    EXPECT_BUILD_LOCK=0 DG_BOARD=p4xc DG_SERIAL_LOCK_HELD=1 timeout 10 "$repo_root/scripts/idf.sh" \
        -C "$scratch/p4" -B "$scratch/p4/build" "$port_option" fake-board-port flash
    grep -q '/hil/boards.py$' "$ARGV_LOG" || fail "P4 did not route through boards.py"
    grep -qx p4xc "$ARGV_LOG" || fail "P4 board identity was lost"
    if grep -q '/hil/board_port.py$' "$ARGV_LOG"; then fail "P4 used frozen EchoEar resolver"; fi
    if grep -q -- '-DSDKCONFIG_DEFAULTS=' "$ARGV_LOG"; then fail "P4 inherited S3 defaults"; fi
done
rm -f "$ARGV_LOG"
EXPECT_BUILD_LOCK=0 DG_BOARD=p4xc timeout 10 "$repo_root/scripts/idf.sh" -B "$scratch/p4/build" monitor
grep -q '/hil/bench.py$' "$ARGV_LOG" || fail "P4 skipped bench wrapper"
rm -f "$ARGV_LOG"
result=0
timeout 10 "$repo_root/scripts/idf.sh" build flash >"$scratch/combined.log" 2>&1 || result=$?
[[ "$result" == 1 && ! -e "$ARGV_LOG" ]] || fail "combined build flash was not refused"
echo "build lock tests passed"
