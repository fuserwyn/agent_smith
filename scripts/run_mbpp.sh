#!/usr/bin/env bash
# Exam-like MBPP run: for each seed, dump a task with the moulinette, run the agent, validate.
#   ./scripts/run_mbpp.sh <model> [provider_url] [seeds...]
#   ./scripts/run_mbpp.sh nvidia/nemotron-3-ultra-550b-a55b:free https://openrouter.ai/api/v1 1 2 3 4 5
set -uo pipefail
MODEL="$1"; URL="${2:-https://openrouter.ai/api/v1}"; shift $(( $# >= 2 ? 2 : 1 )); SEEDS=("${@:-1 2 3 4 5}")
ROOT="$(cd "$(dirname "$0")/.." && pwd)"; MOULINETTE="${MOULINETTE:-$ROOT/moulinette}"; ENV_FILE="${ENV_FILE:-$ROOT/.env}"
OUT="$ROOT/benchmarks/mbpp/$(echo "$MODEL" | tr '/:' '__')"; mkdir -p "$OUT" "$ROOT/cache"
# The moulinette's Docker SDK needs the socket of the current Docker context (Docker Desktop on macOS).
export DOCKER_HOST="${DOCKER_HOST:-$(docker context inspect --format '{{.Endpoints.docker.Host}}' 2>/dev/null)}"
moul() { (cd "$MOULINETTE" && PYTHONPATH=. uv run moulinette_eval "$@"); }
passed=0; total=0
for seed in ${SEEDS[@]}; do
  moul dump mbpp --seed "$seed" --output "$ROOT/cache/mbpp_seed$seed.json" >/dev/null 2>&1
  tid=$(python3 -c "import json;print(json.load(open('$ROOT/cache/mbpp_seed$seed.json'))['task_id'])")
  d="$OUT/$tid"; mkdir -p "$d"; cp "$ROOT/cache/mbpp_seed$seed.json" "$d/task.json"
  (cd "$ROOT" && uv run python -m agent_mbpp --task-file "$d/task.json" --output "$d/solution.json" \
     --model-name "$MODEL" --provider-url "$URL" --env-file "$ENV_FILE" --trace-file "$d/events.jsonl") > "$d/stdout.log" 2> "$d/stderr.log"
  moul validate mbpp "$d/task.json" "$d/solution.json" > "$d/validation.txt" 2>&1
  moul display "$d/solution.json" 2>&1 | sed 's/\x1b\[[0-9;]*m//g' | sed -n '/CONSISTENCY CHECKS/,$p' > "$d/consistency.txt"
  verdict=$(sed 's/\x1b\[[0-9;]*m//g' "$d/validation.txt" | grep -E '^Overall:' | awk '{print $2}')
  total=$((total+1)); [[ "$verdict" == "PASSED" ]] && passed=$((passed+1))
  echo "$(cat "$d/stdout.log" | tail -1) | moulinette: ${verdict:-ERROR} | $(grep -vE '^=+$|CONSISTENCY' "$d/consistency.txt" | tr -s ' \n' ' ')"
done
echo "== $MODEL: $passed/$total passed"
