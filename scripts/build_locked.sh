#!/usr/bin/env bash
set -euo pipefail

if [[ $# == 0 ]]; then
    echo "Usage: $0 <cmd> [args...]" >&2
    exit 2
fi
# DG_BUILD_LOCK_FILE is a test-only override; production builds share one lock.
# The Linux bench keeps its historical lock path; macOS uses the per-user temp dir.
if [[ "$(uname -s)" == Darwin ]]; then lock_dir="/tmp/claude-$(id -u)"; else lock_dir=/tmp/claude-1000; fi
lock_file="${DG_BUILD_LOCK_FILE:-$lock_dir/dg-esp32-build.lock}"
mkdir -p "$(dirname "$lock_file")"
exec {build_lock_fd}>"$lock_file"
if [[ "${DG_BUILD_LOCK_HELD:-}" == 1 ]]; then
    if flock -n "$build_lock_fd"; then
        : # Stale flag: this invocation acquired the previously free lock.
    else
        status=$?
        if [[ "$status" == 1 ]]; then exec "$@"; fi
        echo "Cannot verify build lock (flock exit $status)" >&2
        exit "$status"
    fi
fi
while true; do
    if flock -w 30 "$build_lock_fd"; then break; else status=$?; fi
    if [[ "$status" != 1 ]]; then
        echo "Cannot acquire build lock (flock exit $status)" >&2
        exit "$status"
    fi
    echo "waiting for build lock" >&2
done
export DG_BUILD_LOCK_HELD=1
exec "$@"
