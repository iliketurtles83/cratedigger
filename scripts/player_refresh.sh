#!/usr/bin/env bash
# Trigger music player server maintenance refresh.
# Runs beet update, beet import (as singletons), and registry scan.

ENV_FILE="${HOME}/.config/music-player/music-player.env"
if [ -f "$ENV_FILE" ]; then
    set -a
    # shellcheck disable=SC1090
    source "$ENV_FILE"
    set +a
fi

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "${SCRIPT_DIR}/.." && pwd)"

if [ -f "${REPO_ROOT}/.venv/bin/python" ]; then
    PYTHON_CMD="${REPO_ROOT}/.venv/bin/python"
else
    PYTHON_CMD="python"
fi

if "$PYTHON_CMD" -c "import music.maintenance" 2>/dev/null; then
    echo "Triggering music.maintenance refresh..."
    "$PYTHON_CMD" -m music.maintenance refresh
    exit_code=$?

    if [ $exit_code -eq 75 ]; then
        echo "Refresh was already running (exit code 75). Safe to try again later."
        exit 0
    elif [ $exit_code -ne 0 ]; then
        echo "Refresh failed with exit code $exit_code."
        exit $exit_code
    fi
else
    echo "music.maintenance not found; running sync_beets_db.py directly..."
    "$PYTHON_CMD" "${SCRIPT_DIR}/sync_beets_db.py"
fi

echo "Refresh completed successfully."
