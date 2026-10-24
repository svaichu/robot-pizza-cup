#!/usr/bin/env bash
# Start or stop NormaCore Station. Install first: bash scripts/install.sh
#
# Usage:
#   bash scripts/run.sh        # start (web UI + normfs TCP)
#   bash scripts/run.sh stop   # stop the CLI (and a desktop app, if one is bound to 8888/8889)
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
ENV_FILE="${ELROBOT_ENV:-$ROOT/elrobot.env}"
if [[ -f "$ENV_FILE" ]]; then
  set -a
  # shellcheck disable=SC1090
  source "$ENV_FILE"
  set +a
fi
STATION_DIR="${NORMA_CORE_ROOT:-${STATION_INSTALL_DIR:-$ROOT/norma-core}}"
BIN_DIR="${STATION_DIR}/bin"
STATION_BIN="${BIN_DIR}/station"

stop_station() {
  local cli_bin="$STATION_BIN" stopped=false pids pid cmd

  if [[ "$(uname -s)" == Darwin ]]; then
    if pgrep -qx "NormaCore Station" 2>/dev/null || pgrep -f "NormaCore Station.app" >/dev/null 2>&1; then
      echo "Quitting NormaCore Station.app ..."
      osascript -e 'quit app "NormaCore Station"' 2>/dev/null || killall "NormaCore Station" 2>/dev/null || true
      stopped=true
    fi
  fi

  pids=""
  if [[ -x "$cli_bin" ]]; then
    pids="$(pgrep -f "$cli_bin" 2>/dev/null || true)"
  fi
  while read -r pid; do
    [[ -n "$pid" ]] || continue
    cmd="$(ps -p "$pid" -o command= 2>/dev/null || true)"
    [[ "$cmd" == *station* ]] || continue
    [[ "$cmd" == *NormaCore\ Station.app* ]] && continue
    pids="${pids}${pids:+ }${pid}"
  done < <(lsof -tiTCP:8888 -sTCP:LISTEN 2>/dev/null || true)

  if [[ -n "$pids" ]]; then
    echo "Stopping station process(es): ${pids}"
    # shellcheck disable=SC2086
    kill ${pids} 2>/dev/null || true
    sleep 0.5
    # shellcheck disable=SC2086
    kill -9 ${pids} 2>/dev/null || true
    stopped=true
  fi

  if $stopped; then
    echo "Station stopped."
  else
    echo "No NormaCore Station process found (app or CLI on ports 8888/8889)."
  fi
}

if [[ "${1:-}" == stop ]]; then
  stop_station
  exit 0
fi

if [[ ! -x "$STATION_BIN" ]]; then
  echo "Missing ${STATION_BIN}. Run: bash scripts/install.sh" >&2
  exit 1
fi

cd "$BIN_DIR"
# DATA_DIR="${STATION_DATA_DIR:-$BIN_DIR/station_data}"
exec ./station --config "${STATION_CONFIG:-station.yaml}" --normfs-base-folder "$DATA_DIR" --web --tcp "$@"

DATA_DIR="${STATION_DATA_DIR:-$BIN_DIR/station_data}"
exec ./station --config "${STATION_CONFIG:-station.yaml}" --normfs-base-folder "$DATA_DIR" --web --tcp "$@"

