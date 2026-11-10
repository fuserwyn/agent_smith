#!/usr/bin/env bash
# Local rehearsal of the evaluation (subject chapter VI), without the official exam scripts.
#
#   ./scripts/mock_exam.sh <mbpp|swebench|sandbox|all> --student-path . --moulinette-path ./moulinette --env-file .env \
#       [--model-name M --provider-url URL]            (same model for every part)
#       [--mbpp-model-name M --mbpp-provider-url URL]  (MBPP only; default qwen/qwen3.8-27b on Groq)
#
# Mirrors the official flow: tasks are dumped by the moulinette, the agent runs with the exam CLI,
# the moulinette validates. Results: evaluations/<type>/<YYYY-MM-DD_HH-MM-SS>/<task_id>/
# {task.json, solution.json, stdout.log, stderr.log, validation.txt}.
# Pass thresholds: MBPP 4/5, SWE-bench 2/3 (+ container cleanup), sandbox: every test.
# Each agent run also writes evaluations/.../<task_id>/events.jsonl (AGENT_TRACE_FILE): open it with
#   uv run agent-ui --trace <that file>      (http://127.0.0.1:8765)
set -uo pipefail

TYPE="${1:-all}"; shift || true
STUDENT="."; MOULINETTE="./moulinette"; ENV_FILE=".env"
# Defaults = recommended free exam setup (docs/DEFENSE.*.md, section 1a).
MBPP_MODEL="qwen/qwen3.8-27b"; MBPP_URL="https://api.groq.com/openai/v1"
MODEL="nvidia/nemotron-3-ultra-550b-a55b"; URL="https://integrate.api.nvidia.com/v1"; TASKS=""
while [[ $# -gt 0 ]]; do
  case "$1" in
    --student-path) STUDENT="$2"; shift 2 ;;
    --moulinette-path) MOULINETTE="$2"; shift 2 ;;
    --env-file) ENV_FILE="$2"; shift 2 ;;
    --model-name) MODEL="$2"; MBPP_MODEL="$2"; shift 2 ;;      # one model for every part
    --provider-url) URL="$2"; MBPP_URL="$2"; shift 2 ;;
    --mbpp-model-name) MBPP_MODEL="$2"; shift 2 ;;
    --mbpp-provider-url) MBPP_URL="$2"; shift 2 ;;
    --tasks) TASKS="$2"; shift 2 ;;   # SWE-bench only: space-separated instance ids instead of a random selection
    *) echo "unknown option $1"; exit 2 ;;
  esac
done
STUDENT="$(cd "$STUDENT" && pwd)"; MOULINETTE="$(cd "$MOULINETTE" && pwd)"; ENV_FILE="$(cd "$(dirname "$ENV_FILE")" && pwd)/$(basename "$ENV_FILE")"
export DOCKER_HOST="${DOCKER_HOST:-$(docker context inspect --format '{{.Endpoints.docker.Host}}' 2>/dev/null)}"
STAMP="$(date +%Y-%m-%d_%H-%M-%S)"
G=$'\e[32m'; R=$'\e[31m'; Y=$'\e[33m'; N=$'\e[0m'
moul() { (cd "$MOULINETTE" && PYTHONPATH=. uv run moulinette_eval "$@"); }
verdict() { sed 's/\x1b\[[0-9;]*m//g' "$1" | grep -E '^Overall:' | awk '{print $2}'; }
summary=()

# --------------------------------------------------------------------------- MBPP
exam_mbpp() {
  local dir="$STUDENT/evaluations/mbpp/$STAMP" passed=0
  echo "${Y}=== exam_mbpp: 5 random tasks · model $MBPP_MODEL${N}"
  for i in 1 2 3 4 5; do
    local tmp="$dir/_task$i.json"; mkdir -p "$dir"
    moul dump mbpp --output "$tmp" >/dev/null 2>&1
    local tid; tid=$(python3 -c "import json;print(json.load(open('$tmp'))['task_id'])")
    local d="$dir/$tid"; mkdir -p "$d"; mv "$tmp" "$d/task.json"
    (cd "$STUDENT" && AGENT_TRACE_FILE="$d/events.jsonl" uv run python -m agent_mbpp --task-file "$d/task.json" --output "$d/solution.json" \
       --model-name "$MBPP_MODEL" --provider-url "$MBPP_URL" --env-file "$ENV_FILE") > "$d/stdout.log" 2> "$d/stderr.log"
    moul validate mbpp "$d/task.json" "$d/solution.json" > "$d/validation.txt" 2>&1
    local v; v=$(verdict "$d/validation.txt"); [[ "$v" == "PASSED" ]] && passed=$((passed+1))
    printf "  task %-4s %s  %s\n" "$tid" "$([[ $v == PASSED ]] && echo "${G}PASSED${N}" || echo "${R}${v:-ERROR}${N}")" \
      "$(tail -1 "$d/stdout.log" | sed 's/ -> [^|]*//' | cut -c1-120)"
  done
  local ok=$([[ $passed -ge 4 ]] && echo 1 || echo 0)
  summary+=("MBPP      $passed/5 (need 4)  $([[ $ok == 1 ]] && echo "${G}PASS${N}" || echo "${R}FAIL${N}")  → $dir")
}

# --------------------------------------------------------------------------- SWE-bench
exam_swebench() {
  local dir="$STUDENT/evaluations/swebench/$STAMP" passed=0 leaks=0; mkdir -p "$dir"
  echo "${Y}=== exam_swebench: 3 random tasks from the exam pool · model $MODEL${N}"
  if [[ -n "$TASKS" ]]; then
    python3 -c "import json,sys;json.dump({'instance_ids':sys.argv[1:]},open('$dir/selection.json','w'))" $TASKS
  else
    moul select swebench --count 3 --output "$dir/selection.json" >/dev/null 2>&1
  fi
  for tid in $(python3 -c "import json;print(' '.join(json.load(open('$dir/selection.json'))['instance_ids']))"); do
    local d="$dir/$tid"; mkdir -p "$d"
    moul dump swebench --task-id "$tid" --output "$d/task.json" >/dev/null 2>&1
    local t0=$SECONDS
    (cd "$STUDENT" && AGENT_TRACE_FILE="$d/events.jsonl" uv run python -m agent_swebench --task-file "$d/task.json" --output "$d/solution.json" \
       --model-name "$MODEL" --provider-url "$URL" --env-file "$ENV_FILE") > "$d/stdout.log" 2> "$d/stderr.log"
    local left; left=$(docker ps -aq --filter label=agent-smith | wc -l | tr -d ' '); leaks=$((leaks+left))
    moul validate swebench "$d/task.json" "$d/solution.json" > "$d/validation.txt" 2>&1
    local v; v=$(verdict "$d/validation.txt"); [[ "$v" == "PASSED" ]] && passed=$((passed+1))
    printf "  %-28s %s  wall %4ss  leftover containers: %s  %s\n" "$tid" \
      "$([[ $v == PASSED ]] && echo "${G}PASSED${N}" || echo "${R}${v:-ERROR}${N}")" "$((SECONDS-t0))" "$left" \
      "$(tail -1 "$d/stdout.log" | sed 's/ -> [^|]*//' | cut -c1-110)"
  done
  local n; n=$(python3 -c "import json;print(len(json.load(open('$dir/selection.json'))['instance_ids']))")
  local ok=$([[ $passed -ge 2 && $leaks -eq 0 ]] && echo 1 || echo 0)
  summary+=("SWE-bench $passed/$n (need 2), leftovers $leaks  $([[ $ok == 1 ]] && echo "${G}PASS${N}" || echo "${R}FAIL${N}")  → $dir")
}

# --------------------------------------------------------------------------- sandbox
exam_sandbox() {
  local dir="$STUDENT/evaluations/sandbox/$STAMP" failed=0 total=0; mkdir -p "$dir"
  echo "${Y}=== exam_sandbox: security tests through the sandbox CLI${N}"
  printf '{"max_execution_time_seconds": 3, "max_memory_mb": 128}\n' > "$dir/limits.json"
  printf '{"authorized_imports": ["socket", "os"], "max_execution_time_seconds": 5}\n' > "$dir/permissive.json"
  check() {  # name, expected regex, config, code
    local name="$1" expect="$2" cfg="$3" code="$4" out
    out=$(cd "$STUDENT" && uv run sandbox ${cfg:+"$cfg"} -c "$code" 2>&1); total=$((total+1))
    echo "### $name"$'\n'"$code"$'\n---\n'"$out"$'\n' >> "$dir/sandbox.log"
    if echo "$out" | grep -Eq "$expect"; then printf "  %-44s ${G}OK${N}\n" "$name"
    else printf "  %-44s ${R}FAIL${N}  %s\n" "$name" "$(echo "$out" | tail -1 | cut -c1-80)"; failed=$((failed+1)); fi
  }
  check "import block: import os"                "not allowed"                      "" "import os"
  check "import block: __import__('subprocess')" "not allowed"                      "" "__import__('subprocess')"
  check "import block: random._os escape"        "not allowed"                      "" "import random
random._os.system('id')"
  check "builtin block: eval"                    "NameError"                        "" "eval('1+1')"
  check "builtin block: open-ended exec"         "NameError"                        "" "exec('import os')"
  check "builtin block: __subclasses__ escape"   "not allowed"                      "" "().__class__.__bases__[0].__subclasses__()"
  check "network block (socket not allowed)"     "not allowed"                      "" "import socket"
  check "network block (socket allowed by cfg)"  "network access is disabled"       "$dir/permissive.json" "import socket
socket.create_connection(('1.1.1.1', 80), timeout=2)"
  check "path restrict: read /etc/passwd"        "denied"                           "" "print(open('/etc/passwd').read())"
  check "path restrict: ../ traversal"           "denied"                           "" "print(open('/tmp/agent/../../etc/hosts').read())"
  check "path restrict: os.listdir (os allowed)" "denied"                           "$dir/permissive.json" "import os
print(os.listdir('/'))"
  check "path restrict: allowed dir works"       "^ok$"                             "" "open('/tmp/agent/exam.txt','w').write('ok')
print(open('/tmp/agent/exam.txt').read())"
  check "timeout: while True"                    "TimeoutError"                     "$dir/limits.json" "while True: pass"
  check "timeout: cannot be swallowed"           "TimeoutError"                     "$dir/limits.json" "try:
    while True: pass
except Exception:
    print('caught')"
  check "memory limit: 1 GB allocation"          "MemoryError"                      "$dir/limits.json" "x = bytearray(1024 * 1024 * 1024)"
  check "process block: os.system (os allowed)"  "not allowed"                      "$dir/permissive.json" "import os
os.system('echo pwned')"
  check "secrets: no API key in environment"     "^\\{\\}$|^\\{'PATH'"            "$dir/permissive.json" "import os
print({k: v for k, v in os.environ.items() if 'KEY' in k} or {})"
  # MCP protocol: stdio + HTTP discovery and calls
  local out; total=$((total+1))
  out=$(cd "$STUDENT" && uv run sandbox --mcp-stdio "python mcp_tools_swebench.py" --manual 2>&1)
  if echo "$out" | grep -q "search_function_or_class_definition_in_code(" && echo "$out" | grep -q "final_answer"; then
    printf "  %-44s ${G}OK${N}\n" "MCP stdio: 9 tools discovered + manual"; else printf "  %-44s ${R}FAIL${N}\n" "MCP stdio"; failed=$((failed+1)); fi
  total=$((total+1))
  (cd "$STUDENT" && uv run python mcp_tools_mbpp.py --http --port 8798 >/dev/null 2>&1 &) ; sleep 3
  out=$(cd "$STUDENT" && uv run sandbox --mcp-server http://127.0.0.1:8798/mcp -c "print(run_tests('def f():\n    return 1', ['assert f() == 1']))" 2>&1)
  pkill -f "mcp_tools_mbpp.py --http --port 8798" 2>/dev/null
  if echo "$out" | grep -q "ALL TESTS PASSED"; then printf "  %-44s ${G}OK${N}\n" "MCP streamable HTTP: tool call"
  else printf "  %-44s ${R}FAIL${N}  %s\n" "MCP streamable HTTP" "$(echo "$out" | tail -1)"; failed=$((failed+1)); fi
  local ok=$([[ $failed -eq 0 ]] && echo 1 || echo 0)
  summary+=("Sandbox   $((total-failed))/$total tests  $([[ $ok == 1 ]] && echo "${G}PASS${N}" || echo "${R}FAIL${N}")  → $dir/sandbox.log")
}

case "$TYPE" in
  mbpp) exam_mbpp ;; swebench) exam_swebench ;; sandbox) exam_sandbox ;;
  all) exam_sandbox; exam_mbpp; exam_swebench ;;
  *) echo "usage: $0 <mbpp|swebench|sandbox|all> [options]"; exit 2 ;;
esac
echo; echo "${Y}=== SUMMARY ($STAMP)${N}"; printf '%s\n' "${summary[@]}"
