#!/usr/bin/env bash
# Offline host tests, grouped into lanes. Run only after bench-free permission.
#
#   scripts/host-tests.sh              every lane, in the order --list prints
#   scripts/host-tests.sh --duo        the four duo lanes (no IDF environment)
#   scripts/host-tests.sh peer app     the named lanes only
#   scripts/host-tests.sh --list       the lane names
#
# A lane stops at its first failing command. The other lanes still run, the
# summary names every lane's result, and the exit status is non-zero if any
# lane failed. Each lane's output is kept in $TMPDIR/host-tests-logs/<lane>.log.
#
# Prerequisites are checked by the lanes that need them, never fetched here:
#   duo-art      a python3 on PATH with Pillow (the IDF Python env has none, so
#                under get_idf the lane takes the next python3 on PATH)
#   build-tools  the IDF 6.1 Python env (IDF_PYTHON_ENV_PATH or the pinned path)
#   audio        managed_components/espressif__esp_codec_dev
#                (scripts/idf.sh reconfigure)
#   display      the pinned LCD drivers (DG_BOARD=waveshare_146 scripts/idf.sh
#                reconfigure) and the IDF toolchain on PATH (get_idf)
set -uo pipefail
cd "$(dirname "$0")/.."
export TMPDIR="${TMPDIR:-$HOME/.cache/gap3-tmp}"
mkdir -p "$TMPDIR"
jobs="${JOBS:-4}"
idf_python="${IDF_PYTHON_ENV_PATH:-$HOME/.espressif/python_env/idf6.1_py3.12_env}/bin/python"

all_lanes=(duo-protocol duo-art duo-hil duo-bot p4-touch p4-parity board-neutral build-tools
           diagnostics core client peer board net audio app display hil-wire hil-p4 aec)
duo_lanes=(duo-protocol duo-art duo-hil duo-bot)

py() { OPENBLAS_NUM_THREADS=1 PYTHONDONTWRITEBYTECODE=1 "$@"; }

# pytest in the server environment: PYTHONPATH, extra packages, pytest arguments.
server_pytest() {
    local path="$1" extras=()
    case "$2" in
        full) extras=(--with pyserial --with pyelftools --with idf-component-manager==3.1.2 --with esptool==5.5.0) ;;
        serial) extras=(--with pyserial --with pyelftools) ;;
        plain) ;;
        *) echo "server_pytest: unknown extras '$2'" >&2; return 2 ;;
    esac
    shift 2
    py env PYTHONPATH="$path" uv run --project server --frozen --offline --group dev "${extras[@]}" python -m pytest "$@"
}

# Configure, build and run one CMake host suite under the build lock.
host_cmake() {
    local source="$1" build="$2"
    shift 2
    scripts/build_locked.sh cmake -S "$source" -B "$build" "$@"
    scripts/build_locked.sh cmake --build "$build" -j "$jobs"
    scripts/build_locked.sh ctest --test-dir "$build" --output-on-failure
}

missing() { echo "PREREQUISITE MISSING: $*" >&2; return 3; }

lane_duo_protocol() { host_cmake components/dg_duo/test_host build/host-duo; }
# The first python3 on PATH that has Pillow. get_idf puts its own, without Pillow, first.
pillow_python() {
    local candidate
    while read -r candidate; do
        if "$candidate" -c 'import PIL' 2>/dev/null; then echo "$candidate"; return 0; fi
    done < <(type -ap python3)
    return 1
}
lane_duo_art() {
    local python
    python=$(pillow_python) || missing "a python3 on PATH with Pillow"
    py "$python" -m unittest -v hil/test_duo_art.py hil/test_duo_art_rev1.py
    py "$python" -m unittest -v hil/test_duo_assets.py
}
lane_duo_hil() {
    server_pytest hil:server:scripts full -ra hil/test_duo_identity.py hil/test_duo_bundle.py hil/test_duo_smoke.py \
        hil/test_duo_touch_verify.py hil/test_duo_attempts.py hil/test_duo_records.py --basetemp "$TMPDIR/pytest-duo-hil"
    # The end-to-end harness has module names of its own (cohort, journal, score), so it runs by itself.
    server_pytest hil:server:scripts plain hil/duo_e2e --basetemp "$TMPDIR/pytest-duo-e2e"
}
lane_duo_bot() {
    server_pytest hil:server plain server/test_duo_switch.py server/test_duo_pipeline.py server/test_duo_retirement.py \
        server/test_duo_addressee.py server/test_duo_baseline.py server/test_duo_first_clause.py \
        server/test_duo_context.py server/test_deepgram_think.py server/test_turn_env.py server/test_ice_path_darwin.py \
        --basetemp="$TMPDIR/duo-pytest"
}
lane_p4_touch() {
    server_pytest hil:server:scripts full -ra hil/test_p4_touch_verify.py hil/test_p4_touch_close.py \
        --basetemp "$TMPDIR/pytest-p4-touch-verify"
}
lane_p4_parity() {
    server_pytest hil:server:scripts full -ra hil/test_p4_parity.py hil/test_p4_parity_fix1.py hil/test_p4_parity_fix2.py \
        hil/test_p4_parity_fix3.py hil/test_p4_parity_followup.py hil/test_p4_step16_close.py --basetemp "$TMPDIR/pytest-p4-parity"
}
lane_board_neutral() {
    scripts/check_board_neutral.sh
    py python3 scripts/test_board_neutral.py
    scripts/test_build_locked.sh
}
lane_build_tools() {
    [[ -x "$idf_python" ]] || missing "the IDF Python env at $idf_python"
    py uv run --project hil --frozen --offline python hil/test_boards.py
    py env PYTHONPATH=hil uv run --project hil --frozen --offline python -m unittest hil/test_board_port.py
    py python3 scripts/test_sdkconfig_keydiff.py
    py python3 scripts/test_capture_s3_baselines.py
    python3 scripts/test_check_sdkconfig.py
    py python3 scripts/test_kconfig_boards.py
    py python3 scripts/test_mic_fixture_lines.py
    py python3 scripts/test_dg_select.py
    py "$idf_python" scripts/check_aec_manifest_guard.py
}
lane_diagnostics() {
    host_cmake components/dg_diagnostics/test_host build/host-diagnostics
    py uv run --project hil --frozen --offline python hil/test_dump_verify.py
}
lane_core() { host_cmake third_party/pipecat-client-cxx third_party/pipecat-client-cxx/build-host -DPIPECAT_BUILD_TESTS=ON; }
lane_client() { host_cmake components/pipecat_client/test_host build/host-http; }
lane_peer() { host_cmake components/pipecat_esp_peer/test_host build/host-peer; }
lane_board() { host_cmake components/dg_board/test_host build/host-board; }
lane_net() { host_cmake components/dg_net/test_host build/host-net; }
lane_audio() {
    [[ -f managed_components/espressif__esp_codec_dev/esp_codec_dev_vol.c ]] ||
        missing "managed_components/espressif__esp_codec_dev; run scripts/idf.sh reconfigure"
    host_cmake components/dg_audio/test_host build/host-audio
}
lane_app() { host_cmake main/test_host build/host-app; }
lane_display() {
    command -v riscv32-esp-elf-g++ >/dev/null ||
        missing "the IDF toolchain on PATH (get_idf); the P4 headroom-guard test compiles with it"
    local driver
    for driver in esp_lcd_st77916 esp_lcd_spd2010; do  # The same three places panel_trace.cmake looks in.
        [[ -f managed_components/espressif__$driver/$driver.c || -f managed_components/$driver/$driver.c ||
           -f build/host-display/panel-vendor/$driver/$driver.c ]] ||
            missing "the pinned $driver driver; run DG_BOARD=waveshare_146 scripts/idf.sh reconfigure"
    done
    host_cmake components/dg_display/test_host build/host-display
    python3 hil/test_dgi.py
    py python3 hil/test_p4_display.py
}
lane_hil_wire() {
    server_pytest hil:server plain hil/test_wire_contract.py --basetemp "$TMPDIR/pytest-wire-contract"
    server_pytest hil:server plain hil/test_board_smoke.py --basetemp "$TMPDIR/pytest-board-smoke"
    server_pytest hil:server plain hil/test_bench_launch_docs.py --basetemp "$TMPDIR/pytest-bench-launch-docs"
}
lane_hil_p4() {
    server_pytest hil:server plain hil/test_p4_headless.py --basetemp "$TMPDIR/pytest-p4-headless"
    server_pytest hil:server plain hil/test_p4_console.py --basetemp "$TMPDIR/pytest-p4-console"
    server_pytest hil:server full hil/test_p4_audio.py --basetemp "$TMPDIR/pytest-p4-audio"
    server_pytest hil:server full hil/test_p4_audio_fix4.py --basetemp "$TMPDIR/pytest-p4-audio-fix4"
    server_pytest hil:server serial hil/test_display_session.py hil/test_console_contract.py hil/test_board_smoke_bounds.py \
        hil/test_p4_contention.py hil/test_p4_contention_live.py --basetemp "$TMPDIR/pytest-p4-contention"
    server_pytest hil:server full hil/test_p4_product.py --basetemp "$TMPDIR/pytest-p4-product"
    server_pytest hil:server full hil/test_p4_step14.py hil/test_p4_liveness.py --basetemp "$TMPDIR/pytest-p4-step14"
    server_pytest hil:server full hil/test_p4_heap_attribution.py --basetemp "$TMPDIR/pytest-p4-heap-attribution"
    server_pytest hil:server full hil/test_p4_heap_attribution2.py --basetemp "$TMPDIR/pytest-p4-heap-attribution2"
    server_pytest hil:server full hil/test_p4_seed_derive.py hil/test_p4_defaults_validate.py --basetemp "$TMPDIR/pytest-p4-seed-derive"
}
lane_aec() {
    server_pytest hil:server:scripts full -ra scripts/test_aec_archive_profiles.py scripts/test_aec_image.py \
        scripts/test_p4_aec_bundle.py hil/test_p4_aec_close.py --basetemp "$TMPDIR/pytest-p4-aec-image"
    server_pytest hil:server:scripts full -ra hil/test_p4_aec_policy.py hil/test_p4_aec_measure.py hil/test_p4_aec_product.py \
        hil/test_p4_smoke_rescore.py hil/test_p4_elf_carryover.py hil/test_p4_aec_schema.py hil/test_p4_aec_reselect.py \
        hil/test_p4_aec_guard_rescore.py hil/test_p4_aec_applied.py hil/test_p4_nearend_analysis.py hil/test_p4_nearend_capture.py \
        hil/test_p4_nearend_reanalysis.py hil/test_p4_aec_validation.py hil/test_p4_aec_validation_capture.py \
        --basetemp "$TMPDIR/pytest-p4-aec-measurement"
}

selected=()
for argument in "$@"; do
    case "$argument" in
        --list) printf '%s\n' "${all_lanes[@]}"; exit 0 ;;
        --duo) selected+=("${duo_lanes[@]}") ;;
        *)
            if [[ " ${all_lanes[*]} " != *" $argument "* ]]; then
                echo "Unknown lane '$argument'. Lanes: ${all_lanes[*]}" >&2
                exit 2
            fi
            selected+=("$argument") ;;
    esac
done
[[ ${#selected[@]} -gt 0 ]] || selected=("${all_lanes[@]}")

logs="$TMPDIR/host-tests-logs"
mkdir -p "$logs"
results=()
for lane in "${selected[@]}"; do
    echo "=== lane $lane"
    began=$SECONDS
    ( set -e; "lane_${lane//-/_}" ) 2>&1 | tee "$logs/$lane.log"
    results+=("$lane:${PIPESTATUS[0]}:$((SECONDS - began))")
done

# One line per lane: what ran, and what kind of failure it was if it failed.
python3 - "$logs" "${results[@]}" <<'SUMMARY'
import re, sys
from pathlib import Path
logs, rows, failed = Path(sys.argv[1]), [], 0
for entry in sys.argv[2:]:
    lane, status, seconds = entry.rsplit(":", 2)
    text = (logs / f"{lane}.log").read_text(errors="replace")
    counts = dict(passed=0, failed=0, skipped=0, errors=0)
    for line in text.splitlines():
        if re.search(r"\bin [\d.]+s\b", line) and re.search(r"\d+ (passed|failed|errors?|skipped)", line):  # pytest
            for number, kind in re.findall(r"(\d+) (passed|failed|errors?|skipped)", line):
                counts["errors" if kind.startswith("error") else kind] += int(number)
        elif match := re.search(r"(\d+)% tests passed, (\d+) tests failed out of (\d+)", line):  # ctest
            counts["failed"] += int(match[2]); counts["passed"] += int(match[3]) - int(match[2])
        elif match := re.match(r"Ran (\d+) tests? in", line):  # unittest
            counts["passed"] += int(match[1])
        elif match := re.match(r"(OK|FAILED) \((.*)\)", line):  # unittest detail
            detail = dict(part.split("=") for part in match[2].split(", "))
            for kind, key in (("failed", "failures"), ("errors", "errors"), ("skipped", "skipped")):
                counts[kind] += int(detail.get(key, 0)); counts["passed"] -= int(detail.get(key, 0))
        elif re.search(r"\(Skipped\)\s*$", line):  # ctest lists a skipped test among those that "did not run"
            counts["skipped"] += 1; counts["passed"] -= 1
    if status == "0":
        result = "ok"
    else:
        failed += 1
        result = ("prerequisite missing" if status == "3" and "PREREQUISITE MISSING" in text else
                  "collection error" if counts["errors"] or "ERROR collecting" in text else
                  "failed, a file is missing (external evidence?)" if "FileNotFoundError" in text else "failed")
    rows.append((lane, result, counts, seconds))
print("\n=== host-tests summary")
for lane, result, counts, seconds in rows:
    ran = ", ".join(f"{counts[kind]} {kind}" for kind in ("passed", "failed", "errors", "skipped") if counts[kind])
    print(f"{lane:14} {result:22} {ran or 'no test counts'} ({seconds} s)")
total = {kind: sum(row[2][kind] for row in rows) for kind in ("passed", "failed", "errors", "skipped")}
print(f"{len(rows)} lanes, {failed} failed: " + ", ".join(f"{total[kind]} {kind}" for kind in total))
sys.exit(1 if failed else 0)
SUMMARY
