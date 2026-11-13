#!/usr/bin/env bash
set -euo pipefail
repo_root=$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)
cd "$repo_root"
export TMPDIR="${TMPDIR:-$HOME/.cache/gap3-tmp}"
export PYTHONDONTWRITEBYTECODE=1 OPENBLAS_NUM_THREADS=1
mkdir -p "$TMPDIR"
. "$HOME/esp/esp-idf-v6.1/export.sh" >/dev/null
exec "$repo_root/scripts/build_locked.sh" python "$repo_root/scripts/capture_s3_baselines.py" "$@"
