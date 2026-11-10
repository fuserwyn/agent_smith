#!/usr/bin/env bash
# Remove generated files between evaluations. Never touches .env, moulinette/ sources or committed files.
#
#   ./scripts/clean.sh            # runs, cache, evaluations, sandbox scratch, leftover agent containers
#   ./scripts/clean.sh --traces   # + events.jsonl / *.log kept next to benchmark results (solution.json stays)
#   ./scripts/clean.sh --images   # + SWE-bench Docker images and python:3.11-slim (several GB, re-downloaded on demand)
#   ./scripts/clean.sh --all      # everything above + .venv and moulinette/.venv (recreate with `uv sync`)
#   add --dry-run to only print what would be removed
set -uo pipefail
ROOT="$(cd "$(dirname "$0")/.." && pwd)"; cd "$ROOT"
TRACES=0; IMAGES=0; VENVS=0; DRY=0
for a in "$@"; do
  case "$a" in
    --traces) TRACES=1 ;; --images) IMAGES=1 ;; --all) TRACES=1; IMAGES=1; VENVS=1 ;; --dry-run) DRY=1 ;;
    *) echo "unknown option $a"; exit 2 ;;
  esac
done
run() { if [[ $DRY == 1 ]]; then echo "would: $*"; else echo "+ $*"; "$@"; fi; }

# leftover task containers (label set by src/agent_smith/docker_env.py)
ids=$(docker ps -aq --filter label=agent-smith 2>/dev/null); [[ -n "$ids" ]] && run docker rm -f $ids
pkill -f "agent-ui" 2>/dev/null && echo "+ stopped agent-ui"

for p in cache runs evaluations .pytest_cache /tmp/agent; do [[ -e "$p" ]] && run rm -rf "$p"; done
find . -path ./.venv -prune -o -path ./moulinette -prune -o -name __pycache__ -type d -print | while read -r d; do run rm -rf "$d"; done
find . -path ./.venv -prune -o -name .DS_Store -print | while read -r f; do run rm -f "$f"; done

if [[ $TRACES == 1 ]]; then
  find benchmarks \( -name events.jsonl -o -name stdout.log -o -name stderr.log \) -print | while read -r f; do run rm -f "$f"; done
fi
if [[ $IMAGES == 1 ]]; then
  for img in $(docker images --format '{{.Repository}}:{{.Tag}}' | grep -E '^swebench/sweb\.eval\.|^python:3\.11-slim$'); do run docker rmi "$img"; done
fi
if [[ $VENVS == 1 ]]; then
  for v in .venv moulinette/.venv; do [[ -e "$v" ]] && run rm -rf "$v"; done
fi
echo "done. Kept: .env, moulinette/ sources, committed files ($(git status --short | wc -l | tr -d ' ') uncommitted changes)."
