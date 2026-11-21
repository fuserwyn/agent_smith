#!/usr/bin/env bash
# Re-validate an existing SWE-bench (or MBPP) evaluation on a machine whose Docker is a ROOTLESS PODMAN (42 machines).
#
#   ./scripts/revalidate_podman.sh evaluations/swebench/2026-11-21_13-55-48 [--moulinette-path ./moulinette]
#
# Why: the moulinette copies the patch into the container with Docker's put_archive; the tar entries carry the host
# UID (e.g. 44530196). A rootless podman maps only that UID (as root) and a 65,536-ID subuid range, so it cannot set
# that owner: "copier: put: error setting ownership … lchown … invalid argument", and the validation fails before any
# test runs, whatever the solution. Check:  podman unshare cat /proc/self/uid_map
#
# What it does: for each <task>/task.json + solution.json of the given evaluation (as produced by the official exam
# scripts), it runs the UNCHANGED moulinette `validate`, with scripts/podman_compat on PYTHONPATH: the archive entries
# are recorded as root (mapped to you by podman). Nothing else changes: same moulinette, same solution.json, same
# tests. The verdict goes to <task>/validation_podman.txt next to the original files.
set -uo pipefail
EVAL="${1:?usage: $0 evaluations/<mbpp|swebench>/<date> [--moulinette-path PATH]}"; shift
MOULINETTE="./moulinette"
while [[ $# -gt 0 ]]; do
  case "$1" in
    --moulinette-path) MOULINETTE="$2"; shift 2 ;;
    *) echo "unknown option $1" >&2; exit 2 ;;
  esac
done
HERE="$(cd "$(dirname "$0")/.." && pwd)"
COMPAT="$HERE/scripts/podman_compat"
EVAL="$(cd "$EVAL" && pwd)"; MOULINETTE="$(cd "$MOULINETTE" && pwd)"
export DOCKER_HOST="${DOCKER_HOST:-$(docker context inspect --format '{{.Endpoints.docker.Host}}' 2>/dev/null)}"
[[ "$DOCKER_HOST" == *podman* ]] || echo "note: DOCKER_HOST is not a podman socket ($DOCKER_HOST): the compatibility layer stays inactive" >&2

passed=0; total=0
for d in "$EVAL"/*/; do
  d="${d%/}"
  [[ -f "$d/task.json" && -f "$d/solution.json" ]] || continue
  bench=$(python3 -c 'import json,sys; print(json.load(open(sys.argv[1]))["benchmark"])' "$d/solution.json")
  task=$(basename "$d"); total=$((total + 1))
  (cd "$MOULINETTE" && PYTHONPATH=".:$COMPAT" uv run moulinette_eval validate "$bench" "$d/task.json" "$d/solution.json") \
    > "$d/validation_podman.txt" 2>&1
  verdict=$(sed 's/\x1b\[[0-9;]*m//g' "$d/validation_podman.txt" | grep -E '^Overall:' | awk '{print $2}')
  [[ "$verdict" == "PASSED" ]] && passed=$((passed + 1))
  printf '  %-34s %s   (%s)\n' "$task" "${verdict:-ERROR}" "$d/validation_podman.txt"
done
echo "re-validated: $passed/$total passed"
