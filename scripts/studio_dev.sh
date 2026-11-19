#!/usr/bin/env bash
# Agent Smith Studio, development mode with hot reload:
#   the API server (agent-studio) on 127.0.0.1:8790 + Vite on 127.0.0.1:5173 (proxies /api and /ws to it).
#   ./scripts/studio_dev.sh [PROJECT_DIR]          (or: make studio-dev P=~/code/app)
set -euo pipefail
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
PROJECT="${1:-$ROOT}"
TOKEN="${STUDIO_TOKEN:-dev-$(python3 -c 'import secrets; print(secrets.token_urlsafe(12))')}"
[[ -d "$ROOT/web/node_modules" ]] || (cd "$ROOT/web" && npm ci --no-audit --no-fund)
STUDIO_TOKEN="$TOKEN" uv run --project "$ROOT" --extra studio agent-studio "$PROJECT" --no-browser &
API=$!
trap 'kill $API 2>/dev/null || true' EXIT INT TERM
echo
echo "  Agent Smith Studio (dev): http://127.0.0.1:5173/?token=$TOKEN"
echo
cd "$ROOT/web" && npm run dev
