#!/usr/bin/env bash
# Every run behind BENCHMARK_REPORT.md (free tiers only), in three parallel streams (Docker Desktop with 4 GB RAM
# holds three SWE-bench containers plus their validation). Resumable: finished pairs are skipped.
#
#   ./scripts/run_benchmark_suite.sh            # all streams, logs in benchmarks/swebench/logs/
#   ./scripts/run_benchmark_suite.sh s2         # one stream
#
# Keys needed in .env: NVIDIA_API_KEY, OPENROUTER_API_KEY (":free" models only), COHERE_API_KEY.
set -uo pipefail
ROOT="$(cd "$(dirname "$0")/.." && pwd)"; cd "$ROOT"
RUN="$ROOT/scripts/run_benchmark.sh"
LOGS="$ROOT/benchmarks/swebench/logs"; mkdir -p "$LOGS"
N="https://integrate.api.nvidia.com/v1"; O="https://openrouter.ai/api/v1"; C="https://api.cohere.ai/compatibility/v1"
LIGHTNING="$N|nvidia/nemotron-3.5-lightning-30b-a3b"

s1() {  # NVIDIA: main grid, then two ablations on the reference model
  NO_TABLE=1 MODELS="$LIGHTNING $N|nvidia/nemotron-3-ultra-550b-a55b" "$RUN" v2
  NO_TABLE=1 MODELS="$LIGHTNING" AGENT_ARGS="--prompt-variant minimal" "$RUN" v2-ablation-minimal-prompt
  NO_TABLE=1 MODELS="$LIGHTNING" AGENT_ARGS="--ablation no-patch-check" "$RUN" v2-ablation-no-patch-check
}
s2() {  # OpenRouter ":free" models (the same lightning model as on NVIDIA: provider comparison)
  NO_TABLE=1 MODELS="$O|nvidia/nemotron-3.5-lightning:free $O|nvidia/nemotron-3-ultra-550b-a55b:free
    $O|nvidia/nemotron-3.5-super-vl:free $O|inclusionai/ling-3.2-flash:free $O|baidu/ernie-5.5-flash-preview:free
    $O|google/gemma-4-31b-it:free" "$RUN" v2
}
s3() {  # Cohere, then two more ablations and two repeats of the reference model (run-to-run variance)
  NO_TABLE=1 MODELS="$C|command-a-03-2025 $C|north-mini-code-1.0" "$RUN" v2
  NO_TABLE=1 MODELS="$LIGHTNING" AGENT_ARGS="--ablation full-history" "$RUN" v2-ablation-full-history
  NO_TABLE=1 MODELS="$LIGHTNING" AGENT_ARGS="--ablation no-budget" "$RUN" v2-ablation-no-budget
  NO_TABLE=1 MODELS="$LIGHTNING" "$RUN" v2-repeat-2
  NO_TABLE=1 MODELS="$LIGHTNING" "$RUN" v2-repeat-3
}

if [[ $# -gt 0 ]]; then "$1"; exit; fi
s1 > "$LOGS/s1.log" 2>&1 & s2 > "$LOGS/s2.log" 2>&1 & s3 > "$LOGS/s3.log" 2>&1 &
wait
echo "suite done"
