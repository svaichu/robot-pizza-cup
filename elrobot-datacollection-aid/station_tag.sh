#!/usr/bin/env bash
set -euo pipefail
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
source "$ROOT/scripts/load_config.sh"
export NORMA_CORE_ROOT STATION_HOST
exec "$ROOT/../so101/.venv/bin/python" -u "$ROOT/scripts/station_tag.py" "$@"
