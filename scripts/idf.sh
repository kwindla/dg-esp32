#!/usr/bin/env bash
set -e
# Pin the IDF 6.1 Python env that host-tests.sh and hil/boards.py also use, so
# export.sh does not pick an env from whichever python3 is first on PATH.
pinned_idf_env="$HOME/.espressif/python_env/idf6.1_py3.12_env"
if [[ -z "${IDF_PYTHON_ENV_PATH:-}" && -x "$pinned_idf_env/bin/python" ]]; then
    export IDF_PYTHON_ENV_PATH="$pinned_idf_env"
fi
. "$HOME/esp/esp-idf-v6.1/export.sh" >/dev/null
repo_root=$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)
# Board, variant and overlays are resolved in cmake/dg_select.py, which the
# top-level CMakeLists.txt asks too. It sets board, defaults, config_file,
# default_build and board_args, exports DG_DEMO_AUTOSTART and DG_AEC for
# DG_VARIANT=duo, and refuses an unsupported combination.
selection=$(python3 "$repo_root/cmake/dg_select.py" --shell)
eval "$selection"
# Build separately: the shared bench is never held while compiling.
build_command=("$repo_root/scripts/build_locked.sh" idf.py)
port_action=0
build_action=0
menuconfig_action=0
requested_port=""
build_dir=""
project_dir="$PWD"
option=""
for arg in "$@"; do
    if [[ -n "$option" ]]; then
        case "$option" in
            port) requested_port="$arg" ;;
            build-dir) build_dir="$arg" ;;
            project-dir) project_dir="$arg" ;;
        esac
        option=""
        continue
    fi
    case "$arg" in
        -p|--port) option=port; continue ;;
        -B|--build-dir) option=build-dir; continue ;;
        -C|--project-dir) option=project-dir; continue ;;
        -D|--define-cache-entry|-G|--generator|-b|--baud|--monitor-baud|--print-filter|--print_filter|--timestamp-format|--log-file|-o|--output|--format|--flash-offset|--pad-to-size|--fill-flash-size)
            option=value; continue ;;
        --port=*) requested_port="${arg#*=}"; continue ;;
        -p?*) requested_port="${arg:2}"; continue ;;
        --build-dir=*) build_dir="${arg#*=}"; continue ;;
        -B?*) build_dir="${arg:2}"; continue ;;
        --project-dir=*) project_dir="${arg#*=}"; continue ;;
        -C?*) project_dir="${arg:2}"; continue ;;
        -*) continue ;;
        efuse-common-table|efuse-custom-table) continue ;;
        flash|monitor|erase_flash|*-flash|encrypted-*flash|*-otadata|efuse-*|coredump-*|gdb|gdbtui|gdbgui|openocd) port_action=1 ;;
        build) build_action=1 ;;
        menuconfig) menuconfig_action=1 ;;
    esac
done
if [[ -n "$option" ]]; then echo "Missing IDF option value" >&2; exit 2; fi
# Only the main project owns these defaults/build directories/locks. A port
# action may name an already-built external project; a config/build may not.
if [[ -n "$default_build" && "$port_action" == 0 ]]; then
    if [[ -z "$project_dir" || $(python3 -c 'import os, sys; print(os.path.realpath(sys.argv[1]))' "$project_dir") != "$repo_root" ]]; then
        echo "DG_BOARD=$board: non-port actions require the main project (-C $repo_root); use build_locked.sh for other projects" >&2
        exit 1
    fi
fi
build_args=()
if [[ -n "$default_build" ]]; then
    if [[ -z "$build_dir" ]]; then
        build_dir="$default_build";build_args=(-B "$build_dir")
    fi
    effective_build="$build_dir"
    if [[ "$effective_build" != /* ]]; then effective_build="$PWD/$effective_build"; fi
    config_file="$effective_build/sdkconfig"
fi
if [[ "$port_action" == 1 ]]; then
    if [[ "${DG_BOARD:-echoear}" == waveshare_146 ]]; then
        echo "waveshare_146: build-only board; hardware access refused" >&2; exit 1
    fi
    if [[ "$build_action" == 1 ]]; then
        echo "Run build separately before taking the EchoEar bench lock" >&2; exit 1
    fi
    if [[ "${DG_BUILD_LOCK_HELD:-}" == 1 ]]; then
        echo "Refusing serial-port actions while DG_BUILD_LOCK_HELD=1; run separately after releasing the build lock" >&2
        exit 1
    fi
    # hil/bench.py and the other bench tools open this one host-wide lock path
    # and expect its directory to exist; macOS clears /tmp at restart.
    mkdir -p /tmp/claude-1000
    if [[ "${DG_SERIAL_LOCK_HELD:-}" != 1 && -z "${DG_BENCH_LOCK_FD:-}" ]]; then
        exec python "$repo_root/hil/bench.py" "$0" "$@"
    fi
    # A port action re-runs CMake with this selection's defaults. If the build
    # directory was configured from another selection (for example built with
    # DG_DEMO_AUTOSTART=1 DG_PUBLIC_DEMO=1 but flashed without them), idf.py
    # would silently rebuild a different image and flash that instead.
    if [[ "$board" != p4xc ]]; then
        # Without -B, idf.py builds in the project directory's build/.
        guard_dir="${build_dir:-$project_dir/build}"
        [[ "$guard_dir" == /* ]] || guard_dir="$PWD/$guard_dir"
        cache="$guard_dir/CMakeCache.txt"
        # An explicit SDKCONFIG_DEFAULTS definition (the S3 AEC overlay) wins over the
        # selection; as in idf.py, the last definition counts.
        expected="$defaults"
        previous=
        for arg in "$@"; do
            entry=
            case "$arg" in
                -D?*) entry="${arg#-D}" ;;
                --define-cache-entry=*) entry="${arg#--define-cache-entry=}" ;;
                *) if [[ "$previous" == -D || "$previous" == --define-cache-entry ]]; then entry="$arg"; fi ;;
            esac
            # CMake also accepts a typed definition, NAME:TYPE=value.
            if [[ "$entry" =~ ^SDKCONFIG_DEFAULTS(:[A-Z]+)?=(.*)$ ]]; then expected="${BASH_REMATCH[2]}"; fi
            previous="$arg"
        done
        if [[ -f "$cache" ]]; then
            cached=$(sed -n 's/^SDKCONFIG_DEFAULTS:[A-Z]*=//p' "$cache")
            if [[ -n "$cached" && "$cached" != "$expected" ]]; then
                echo "Refusing $guard_dir: built with SDKCONFIG_DEFAULTS=$cached," >&2
                echo "  but this invocation gives $expected." >&2
                echo "  Re-run with the DG_* variables (and any -D SDKCONFIG_DEFAULTS=...) used for the build." >&2
                exit 1
            fi
        fi
    fi
    if [[ "${DG_BOARD:-echoear}" == p4xc ]]; then
        if [[ "$build_dir" != /* ]]; then
            echo "P4 hardware actions require an absolute -B for an already-built P4 project" >&2
            exit 1
        fi
        verified_port=$(python "$repo_root/hil/boards.py" --board p4xc --build "$build_dir" "$requested_port")
    else
        verified_port=$(python "$repo_root/hil/board_port.py" "$requested_port")
    fi
    export ESPPORT="$verified_port"
    build_command=(idf.py)
fi
if [[ "$menuconfig_action" == 1 ]]; then
    echo "menuconfig holds the build lock until it exits" >&2
fi
config_args=("-DSDKCONFIG=$config_file")
defaults_args=("-DSDKCONFIG_DEFAULTS=$defaults")
# Port-only actions against an already-built P4 project must not rewrite its
# config/defaults/lock. This also preserves explicit external-project routing.
if [[ "$board" == p4xc && "$port_action" == 1 ]]; then
    config_args=();defaults_args=();board_args=()
fi
for arg in "$@"; do
    if [[ "$arg" == -DSDKCONFIG=* ]]; then config_args=(); break; fi
done
exec "${build_command[@]}" "${build_args[@]}" "${board_args[@]}" "${defaults_args[@]}" "${config_args[@]}" "$@"
