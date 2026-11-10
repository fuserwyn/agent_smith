#!/usr/bin/env bash
# Run every model in MODELS on every SWE-bench task in TASKS, validate with the moulinette,
# and keep solution.json + validation output under benchmarks/<run-name>/.
#
#   ./scripts/run_benchmark.sh main            # run name (e.g. main, ablation-before, ablation-after)
#
# Requirements: Docker running, moulinette next to this repo (or MOULINETTE=path), keys in .env.
set -uo pipefail

RUN_NAME="${1:-main}"
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
MOULINETTE="${MOULINETTE:-$ROOT/moulinette}"
ENV_FILE="${ENV_FILE:-$ROOT/.env}"
OUT="$ROOT/benchmarks/swebench/$RUN_NAME"
# The moulinette's Docker SDK needs the socket of the current Docker context (Docker Desktop on macOS).
export DOCKER_HOST="${DOCKER_HOST:-$(docker context inspect --format '{{.Endpoints.docker.Host}}' 2>/dev/null)}"
CACHE="$ROOT/cache"
TASKS=(${TASKS:-sympy__sympy-14711 sympy__sympy-13480 pydata__xarray-4629})
# "provider_url|model" pairs — edit to the models you benchmark (free tiers only).
MODELS=(${MODELS:-
  "https://openrouter.ai/api/v1|nvidia/nemotron-3-ultra-550b-a55b:free"
  "https://openrouter.ai/api/v1|cohere/north-mini-code:free"
  "https://openrouter.ai/api/v1|nvidia/nemotron-3.5-lightning:free"
  "https://openrouter.ai/api/v1|nvidia/nemotron-3.5-super-vl:free"
  "https://openrouter.ai/api/v1|dots-studio/dots-3-note-preview:free"
})

mkdir -p "$CACHE" "$OUT"
for task in "${TASKS[@]}"; do
  task_file="$CACHE/$task.json"
  [[ -f "$task_file" ]] || (cd "$MOULINETTE" && PYTHONPATH=. uv run moulinette_eval dump swebench --task-id "$task" --output "$task_file")
  for entry in "${MODELS[@]}"; do
    url="${entry%%|*}"; model="${entry#*|}"
    slug="$(echo "$model" | tr '/:' '__')"
    dir="$OUT/$slug/$task"; mkdir -p "$dir"
    echo "=== $model on $task"
    (cd "$ROOT" && uv run python -m agent_swebench --task-file "$task_file" --output "$dir/solution.json" \
        --model-name "$model" --provider-url "$url" --env-file "$ENV_FILE" --trace-file "$dir/events.jsonl") \
        > "$dir/stdout.log" 2> "$dir/stderr.log"
    (cd "$MOULINETTE" && PYTHONPATH=. uv run moulinette_eval validate swebench "$task_file" "$dir/solution.json") \
        > "$dir/validation.txt" 2>&1
    sed 's/\x1b\[[0-9;]*m//g' "$dir/validation.txt" | grep -E "^Overall" || true
    echo "    leftover containers: $(docker ps -aq --filter label=agent-smith | wc -l | tr -d ' ')"
  done
done
(cd "$ROOT" && uv run python scripts/benchmark_table.py "$OUT")
