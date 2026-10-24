#!/usr/bin/env bash
# Export tags / convert Station Parquet. Viewer is opt-in with --view.
set -euo pipefail
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
source "$ROOT/scripts/load_config.sh"
export NORMA_CORE_ROOT STATION_HOST
PYTHON="$ROOT/../so101/.venv/bin/python"
[[ -x "$PYTHON" ]] || { echo 'Missing so101/.venv Python environment.' >&2; exit 1; }
exec "$PYTHON" "$ROOT/scripts/station_dataset.py" "$@"
