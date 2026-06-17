#!/usr/bin/env bash
# Daily EdgeRadar run: make sure the stack is up, refresh data, post signals to Discord.
# Designed to be invoked by cron (which has a minimal environment), so we set PATH
# explicitly and cd into the project. Requires Docker Desktop to be running.
set -uo pipefail

export PATH="/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin:$PATH"

# Project root = the directory above this script.
cd "$(dirname "$0")/.." || exit 1

echo "===== EdgeRadar daily run: $(date) ====="
docker compose up -d >/dev/null 2>&1 || true   # ensure the stack is running
make notify
echo "===== done: $(date) ====="
