#!/usr/bin/env bash
# Poll for actionable GitHub issues and dispatch git-contribute via Claude Code.
# Usage: ./run_loop.sh [owner/repo]

set -euo pipefail

SCRIPT_DIR="$(dirname "$(realpath "$0")")"
REPO="${1:-}"

while true; do
    echo "$(date '+%Y-%m-%d %H:%M:%S') — Checking for actionable work..."

    if python3 "$SCRIPT_DIR/check_actionable.py" $REPO; then
        echo "Work found — launching git-contribute..."
        claude --dangerously-skip-permissions "run /git-contribute" --print
        echo "$(date '+%Y-%m-%d %H:%M:%S') — Done. Sleeping 5 seconds..."
        sleep 5
    else
        echo "$(date '+%Y-%m-%d %H:%M:%S') — No work found. Sleeping 5 minutes..."
        sleep 300
    fi
done
