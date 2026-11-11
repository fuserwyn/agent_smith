# Agent Smith — defense manual (EN)

> Russian version: [DEFENSE.ru.md](DEFENSE.ru.md).
> Goal: walk into the evaluation able to run every part, explain every design decision and make
> small live changes in 2–5 minutes (subject VI.4). Read it once end to end, then rehearse section 6.

---

## 0. What the evaluator checks (subject VI)

| Script | What happens | Pass |
|---|---|---|
| `exam_mbpp.sh` | 5 random MBPP tasks: dump → `python -m agent_mbpp` → validate | 4/5, limits respected |
| `exam_swebench.sh` | 3 random SWE-bench tasks: dump → `python -m agent_swebench` → validate → **container cleanup** | 2/3, limits respected |
| `exam_sandbox.sh` | import block, builtin block, network block, path restriction, timeout, memory limit, MCP protocol | **all** |
| Review | mandatory tools, agent loop, sandbox security, benchmark report, code quality, **live modifications**, AI safety | — |

Hard limits (cumulative per task, reasoning tokens included):
MBPP 10 iterations · 6,000 input · 1,500 output · 120 s. SWE-bench 30 · 300,000 · 10,000 · 900 s.
They live in `src/agent_smith/models.py` (`MBPP_LIMITS`, `SWEBENCH_LIMITS`) and match the moulinette code
(`moulinette/moulinette/models.py`, `MetricsLimits`). The moulinette README shows older, smaller numbers: the code wins.

Instant fails: an API key in the code; solution from PRs/issues/internet; bypassing the sandbox; a crash.

---

## 1. Preparation checklist (the day before)

- [ ] `git status` clean, everything pushed, you are on the branch that will be graded.
- [ ] First line of `README.md` contains the correct 42 login(s).
- [ ] `.env` exists locally with keys of **free-tier** models (several keys per provider if possible), **not committed**
      (`git check-ignore -v .env` must print a rule).
- [ ] `uv sync && uv run pytest -q` → all green.
- [ ] Docker is running; `export DOCKER_HOST=unix://$HOME/.docker/run/docker.sock` on macOS so the moulinette's
      Docker SDK finds Docker Desktop.
- [ ] SWE-bench images of the starter tasks pulled in advance (they are several GB, x86_64; on Apple Silicon they run emulated).
- [ ] `BENCHMARK_REPORT.md` filled with real numbers; backing `solution.json` files committed under `benchmarks/`.
- [ ] You can open `ui/index.html` (or the published page) to show a trace.
- [ ] You rehearsed section 6 (live modifications) at least once, then `git checkout .`.

---

## 1a. Recommended exam setup (all free, verified by the mock exam)

| Exam | Model | Provider URL | Key | Mock exam result |
|---|---|---|---|---|
| MBPP | `qwen/qwen3.8-27b` | `https://api.groq.com/openai/v1` | `GROQ_API_KEY` | 5/5, 1 iteration, ~1 s per task |
| MBPP (backup) | `north-mini-code-1.0` | `https://api.cohere.ai/compatibility/v1` | `COHERE_API_KEY` (trial: 1000 calls/month, 20/min) | 5/5, 1 iteration, ~1 s per task |
| SWE-bench | `nvidia/nemotron-3.5-lightning-30b-a3b` | `https://integrate.api.nvidia.com/v1` | `NVIDIA_API_KEY` | benchmark 3/3, 146 s for 3 tasks |
| SWE-bench (backup) | `nvidia/nemotron-3-ultra-550b-a55b` | `https://integrate.api.nvidia.com/v1` | `NVIDIA_API_KEY` | mock exam 3/3 (random tasks), benchmark 2/3 |

Why not one model for both: Groq allows only 8,000 tokens per minute (a SWE-bench prompt is 5–15k tokens); NVIDIA is
slower but has no such limit. Free OpenRouter models: only 50 requests/day per account. Mistral: API keys need a paid
plan. Cerebras: needs a payment method. Reasoning is disabled per provider in `config/models.json` (`extra_body`).
Rehearse with `./scripts/mock_exam.sh all --student-path . --moulinette-path ./moulinette --env-file .env`.

## 1b. Cleaning up between evaluations

`./scripts/clean.sh` removes generated files only (never `.env`, `moulinette/` sources or committed files):
`cache/ runs/ evaluations/ /tmp/agent`, caches and leftover `agent-smith` containers. Options: `--traces`
(benchmark traces/logs, `solution.json` stays), `--images` (SWE-bench images, GBs), `--all` (+ virtualenvs,
recreate with `uv sync`), `--dry-run`. Do **not** use `git clean -X`: it deletes `.env` and `moulinette/`.

## 2. Demo script (what to run, what to say)

### 2.1 Sandbox (exam_sandbox.sh categories)

```bash
uv run sandbox sandbox_template.json
```

Type these, one per prompt; expected result on the right:

| Input | Expected | Layer that stops it |
|---|---|---|
| `import os` | `SecurityError: … import of 'os' is not allowed` | AST policy + import allowlist |
| `__import__('os')` | `SecurityError: use of name '__import__'` | AST policy (dunder names) |
| `eval('1+1')` | `NameError: name 'eval' is not defined` | restricted builtins |
| `().__class__.__bases__[0].__subclasses__()` | `SecurityError … '__subclasses__'` | AST policy (dunder attributes) |
| `getattr((), '__class__')` | `SecurityViolation` | safe `getattr` |
| `import random; random._os.system('id')` | `AttributeError: access to private attribute 'random._os'` | module proxy |
| `open('/etc/passwd').read()` | `PermissionError: Sandbox: access to path '/etc/passwd' is denied` | audit hook |
| `open('/tmp/agent/x.txt','w').write('ok')` | `Out: 2` (allowed directory) | — |
| `while True: pass` | `TimeoutError … [NOTE] … PARTIAL` after 30 s | SIGALRM (+ host hard kill) |
| `x = bytearray(1024*1024*1024)` | `MemoryError: … memory limit of 512 MB` | RLIMIT_AS (Linux) / host RSS watchdog |
| `final_answer("done")` | `[final_answer] done` | built-in, not MCP |
| `exit` / Ctrl+D | REPL ends cleanly | — |

Network: `socket` is not in the allowlist. To prove the second layer, start with a config that allows it:
`{"authorized_imports": ["socket"]}` then `import socket; socket.create_connection(("1.1.1.1", 80))` →
`PermissionError: Sandbox: network access is disabled (socket.connect)` (audit hook).

MCP protocol:

```bash
uv run sandbox --mcp-stdio "python mcp_tools_mbpp.py" sandbox_template.json --manual   # generated tool manual
uv run sandbox --mcp-stdio "python mcp_tools_swebench.py" sandbox_template.json        # 9 tools as functions
python mcp_tools_mbpp.py --http --port 8000 &                                          # streamable HTTP
uv run sandbox --mcp-server http://127.0.0.1:8000/mcp
```

Inside: `print(run_tests("def f(): return 1", ["assert f() == 1"]))`, `print(list_resources())`.

What to say: *"The sandbox is a separate process with an empty environment. Five layers: static AST check,
restricted builtins, read-only module proxies, an audit hook that cannot be removed, and OS limits enforced
by the parent. MCP tools run outside the sandbox: the worker sends a JSON request to the host, which owns the MCP
client. The timeout is paused during tool calls."*

### 2.2 MBPP

```bash
cd moulinette && uv run moulinette_eval dump mbpp --output ../cache/mbpp_task.json && cd ..
uv run python -m agent_mbpp --task-file cache/mbpp_task.json --output cache/mbpp_solution.json \
  --model-name "<free model>" --provider-url "https://openrouter.ai/api/v1" --env-file .env --trace-file runs/live.jsonl
cd moulinette && uv run moulinette_eval validate mbpp ../cache/mbpp_task.json ../cache/mbpp_solution.json
uv run moulinette_eval display ../cache/mbpp_solution.json      # consistency checks must be clean
```

(on this Mac the moulinette needs `PYTHONPATH=.` before `uv run`.)

Point out in `solution.json`: `system_prompt`, every step's `llm_output` / `sandbox_input` / `sandbox_output` /
`retries` / `api_url` / `model_name`, totals = sums of steps.

### 2.3 SWE-bench

```bash
uv run python -m agent_swebench --task-file cache/swebench_task.json --output cache/swebench_solution.json \
  --model-name "<free model>" --provider-url "https://openrouter.ai/api/v1" --env-file .env --trace-file runs/swe.jsonl
docker ps -a --filter label=agent-smith      # must be EMPTY after the run: container cleaned up
cd moulinette && uv run moulinette_eval validate swebench ../cache/swebench_task.json ../cache/swebench_solution.json
```

### 2.4 Visualiser

`uv run agent-ui --trace runs/live.jsonl` → http://127.0.0.1:8765, start an agent with the same `--trace-file`.
Use it to show the loop, a tool call hop by hop, and the diff between the LLM output and the executed code.

---

## 3. Architecture in 60 seconds

```
task.json → CLI (agent_mbpp / agent_swebench) → Orchestrator (agent/loop.py) ⇄ LLM (llm/)
                                                    │ raw text
                                                    ▼
                                           Code extraction (extraction/)
                                                    │ python
                        ┌──────────────── Sandbox ─────────────────┐
                        │ worker process ── tool_call ──▶ host ────┼─ MCP client ── stdio/HTTP ──▶ MCP server
                        │ final_answer() built in    ◀── result ── │                               (mcp_tools_*.py)
                        └───────────────────────────────────────────┘                                    │ docker exec
                                                                                                    /testbed container
```

| Question | Answer | File |
|---|---|---|
| Where is the loop? | `CodeAgent.run` | `src/agent_smith/agent/loop.py` |
| Where are limits checked? | `Budget.can_start_step` before every call | `src/agent_smith/agent/budget.py` |
| Where is the prompt? | `mbpp_system_prompt`, `swebench_system_prompt` | `src/agent_smith/agent/prompts.py` |
| Where is the tool manual generated? | `build_manual` from MCP schemas | `src/agent_smith/sandbox/manual.py` |
| Where is code extracted? | `extract_code` | `src/agent_smith/extraction/extractor.py` |
| Where does code run? | `Worker.run` (child), `Sandbox.execute` (parent) | `src/agent_smith/sandbox/worker.py`, `host.py` |
| Where are tools discovered? | `MCPClient._discover` → `tool_spec_from_schema` | `src/agent_smith/mcp_client/client.py` |
| Where are tools implemented? | `SWEBenchTools`, `mbpp_tools.run_tests` | `src/agent_smith/tools/` |
| Where is key rotation? | `KeyPool`, `LLMClient.chat` | `src/agent_smith/llm/key_pool.py`, `client.py` |
| Where is Docker handled? | `DockerEnv` | `src/agent_smith/docker_env.py` |
| Where are the models of the subject? | `SandboxConfig`, `StepMetrics`, `SolutionOutput`… | `src/agent_smith/models.py` |

---

## 4. Deep dives (be able to explain each)

### 4.1 Agent loop (`agent/loop.py`)
1. `_messages()` builds: system prompt, task, then `(assistant, "Observation: …")` pairs. Only the last
   `keep_recent_steps` observations are sent in full; older ones are shortened (`old_observation_chars`).
2. `Budget.can_start_step(estimate)`: iterations left, time left > 5 s, estimated input ≤ remaining input
   budget (estimate = chars/3 × 1.1, conservative), output left ≥ 64. If the input estimate is too big, the
   loop first compacts all old observations, then stops if it still does not fit. **It never sends a call that
   could exceed a limit.**
3. `max_tokens = min(max_output_tokens_per_call, output_left)`; HTTP timeout = time left.
4. `llm.chat(..., stop=STOP_SEQUENCES)`.
5. `extract_code()`; no code → `NO_CODE_FEEDBACK` (counts as an iteration).
6. `sandbox.execute(code)`; observation = notes + stdout + `Out: <last expression>` + error + sandbox notes.
7. `final_answer` → `SolutionOutput(success=True)`. Budget exhausted → `fallback_answer()` (MBPP: last code that
   passed `run_tests`, else last tried; SWE-bench: current `get_patch()`), `success=False`, reason in `error`.

Why `success=False` on fallback? Honesty: it is "whether the agent believes it solved the task". The moulinette does
not read it (it validates the solution itself), so the candidate can still pass.

### 4.2 Five feedback situations (subject V.1, red box) — where they are handled
| Situation | Message | Code |
|---|---|---|
| No valid code block | `[ERROR] No valid code block was found…` with the expected format | `extraction/extractor.py` `NO_CODE_FEEDBACK` |
| Malformed block interpreted anyway | `[NOTE] Your ```python block was not closed…`, untagged block, `>>>` prompts, dedent, XML/JSON/ReAct converted | `extract_code` notes |
| Timeout, partial output | output streamed so far + `[NOTE] Execution hit the timeout; the output above is PARTIAL.` | `host.py` `ExecutionResult.render` |
| Output truncated | `[NOTE] Printed output exceeded the sandbox limit…`; tools say `[truncated: …]`; long observations `[... observation truncated …]` | worker `StreamingStdout`, `swebench_tools.truncate_middle`, `loop.truncate_observation` |
| Edit introduced a syntax error | `Error: the edit was NOT applied because it would introduce a syntax error … SyntaxError … at line N` + context | `swebench_tools.SWEBenchTools._syntax_problem` |

Also: sandbox restarted (variables lost), repeated identical code, budget line on SWE-bench.

### 4.3 Code extraction formats (`extraction/extractor.py`)
Order: ```python blocks (several → joined, with a note) → unclosed fence → untagged fence that parses →
`<code>` → XML `<invoke name><parameter>` → Hermes `<tool_call>{json}` → ReAct `Action / Action Input`.
Non-Python formats become `result = tool(k=v)\nprint(result)` (or `final_answer(...)`). Stop sequences:
`<end_code>`, `\nObservation:`, `</tool_call>`, `</function_calls>` (4 = provider maximum). They prevent the model from
writing a fake Observation; the extractor handles the closing tags removed by the stop.

### 4.4 Sandbox (`sandbox/worker.py`, `sandbox/host.py`)
**Why a separate process?** In-process you cannot stop a C-level loop (`10**10**8`), cannot apply OS memory limits,
and the agent's API keys are in `os.environ`. A child gives: hard kill, `setrlimit`, empty environment, own session
(Ctrl+C reaches only the parent). Cost: the tool-call proxy (which is also the security boundary MCP needs).

Layers, in the order code meets them:
1. **AST policy** (`PolicyChecker`): rejects dunder attributes except a safe list (`__init__`, `__name__`…),
   frame/code attributes (`f_globals`, `tb_frame`, `gi_frame`…), dunder names (`__builtins__`, `__import__`),
   imports outside `authorized_imports` (`math.*` = sub-modules), relative imports.
2. **Restricted builtins** (`REMOVED_BUILTINS`): no `eval exec compile input breakpoint globals locals vars help exit quit`;
   `getattr/hasattr/setattr/delattr` refuse forbidden names; `__import__` is `guarded_import`.
3. **Module proxies** (`make_module_proxy`): imported modules are read-only views; `_private` names and
   non-allowlisted sub-modules are hidden (`random._os`, `typing.sys`, `collections._sys`). The real module lives in
   a closure, unreachable without dunder access.
4. **Audit hook** (`Worker.audit_hook`, `sys.addaudithook`, cannot be removed): `open` outside
   `allowed_directories` (realpath, so `..` and symlinks do not escape; stdlib read-only allowed for lazy imports),
   raw fds (protocol), `os.listdir/remove/rename…` outside allowed dirs, all `socket.*`, `subprocess.*`,
   `os.system/exec/fork/kill`, `ctypes`, `sys.settrace`, imports of hard-denied modules (`os`, `sys`, `subprocess`…)
   unless explicitly allowlisted.
5. **Limits**: `signal.setitimer` raises `SandboxTimeout(BaseException)` — `except Exception` cannot swallow it;
   paused in `call_host_tool` during MCP calls. The host kills the worker if it does not answer within
   timeout + 2 s (C code), then restarts it and says the state was reset. Memory: `RLIMIT_AS` on Linux
   (MemoryError inside, state kept); on every OS the host polls RSS every 100 ms and checks the peak RSS sent
   with each result (catches allocations faster than the poll).

Also: protocol on duplicated private fds, fd 0/1 → `/dev/null` (code cannot forge messages); `SystemExit` /
`KeyboardInterrupt` from user code are re-raised by `Sandbox.execute` so the agent loop stops properly (and still
writes `solution.json`); `final_answer` is injected by the sandbox, it is **not** an MCP tool; the last
expression value is shown like a REPL (`Out: …`).

**Timeout only for sandbox code** (subject): the worker pauses its itimer during a tool call and the host
excludes tool time from its hard-kill deadline (`tool_time`). Test: `test_tool_time_does_not_count_toward_sandbox_timeout`.

### 4.5 MCP
- Client: `MCPClient` runs the async SDK in a background event loop; `connect()` → `initialize` → `list_tools`
  → `ToolSpec` (name, description, params with JSON-schema types, required first). Nothing hard-coded: the
  **unknown MCP server** of the evaluation works the same way (test: `test_unknown_server_tools_are_discovered_dynamically`).
- Wrappers: `Worker.make_tool` maps positional → named args from the schema, checks missing/unknown args.
- Resources/prompts → `list_resources() / read_resource(uri) / list_prompts() / get_prompt(name, arguments)`.
- Transports: stdio (`--mcp-stdio`) and streamable HTTP (`--mcp-server URL`, servers: `--http --port`).
- Servers return `CallToolResult` with `_meta["agent_smith"]` (commands, exit codes, timings, provenance) — ignored
  by other clients, used by the UI.

### 4.6 Mandatory tools (`tools/swebench_tools.py`)
Executor: `DockerExecutor` (`docker exec` into `SWEBENCH_CONTAINER`) or `LocalExecutor` (`TESTBED_PATH`) — same
interface, so tools are testable without Docker (`tests/test_swebench_tools.py`).
- `read_file` → `N: line`, 400 lines per call + paging hint.
- `edit_file` → exactly one match required; 0 → similar lines; >1 → line numbers; syntax check → **rejected and
  not written** if it breaks Python; drops stale `.pyc` (same-size edit within the same second would be ignored by
  Python's mtime+size cache — found by a test).
- `list_files` (`find`, prunes `.git`…), `search_code` (`/abs/path.py:N content`, invalid regex → literal),
  `search_function_or_class_definition_in_code` (`def|class name`), `find_references` (whole word, given file first).
- `run_tests` → task eval script (from `SWEBENCH_EVAL_SCRIPT_FILE`, else `/eval.sh`), keeps the part between
  `>>>>> Start/End Test Output` like the moulinette, summary of failing tests + tail.
- `get_patch` → `git -c core.fileMode=false diff`. `run_command` → exit code, stdout, stderr, timeout.

MBPP `run_tests(code, tests=None)`: tests come from **task.json** (moulinette, via `MBPP_TASK_FILE`), not from the
LLM; the candidate + one `try/except` block per assert runs in a fresh sandbox; reports got/expected.
The moulinette hides the first test: our prompt asks for a general solution.

### 4.7 LLM layer (`llm/`)
- `OpenAICompatProvider`: one HTTP client for every OpenAI-compatible API; error classes decide retry/rotate/disable.
- `KeyPool`: round-robin; 429 → cooldown (`Retry-After`) and next key; 401/403/402 → key disabled; all cooling →
  wait for the soonest; keyless for Ollama.
- `LLMClient.chat`: every HTTP attempt is a request (`total_requests`), failed attempts = `retries`; backoff with
  jitter capped by the deadline; optional fallback endpoints (`--fallback`, `config/models.json`).
- Keys only from env/.env: `<PROVIDER>_API_KEY`, `_2…_9`, `<PROVIDER>_API_KEYS=a,b`; provider found by URL host.
- Usage: `prompt_tokens`/`completion_tokens` (reasoning tokens are part of completion tokens); missing usage →
  estimate + warning.

### 4.8 SWE-bench specifics (`agent_swebench`, `docker_env.py`)
Option (b) of the subject: sandbox on the host, tools bridge into Docker. Container: `--network none` (the agent
cannot download a fix: AI-safety), label `agent-smith=1`, `--platform linux/amd64` for x86_64 images, removed in
`finally`, `atexit`, and on SIGTERM (converted to `SystemExit`). The clock starts after the image is available
(pulling is infrastructure). The validator applies the patch in a **new** container, so leftovers would not help,
but they must still be cleaned.

### 4.9 AI safety (VI.4.1 — violations = 0)
System prompt "Integrity" section; container without network; tools only see the repository; every step logged
(`llm_output`, `sandbox_input`, `sandbox_output`) so the evaluator can verify exploration; no web/PR tools exist.

---

## 5. Likely questions and short answers

1. **Why not smolagents/LangGraph?** Forbidden (orchestration). Our loop is ~200 lines in `agent/loop.py`; we only use
   the official MCP SDK (protocol, not orchestration), pydantic, httpx.
2. **Why code actions instead of JSON tool calls?** Variables persist, loops/conditions, several tool calls per step →
   fewer iterations and tokens (CodeAct paper).
3. **How do you know the model did not invent an observation?** Stop sequences + the observation is only what the
   sandbox returned; `llm_output` in `solution.json` is the raw text before extraction.
4. **What if the model uses another format?** Extractor converts XML/JSON/ReAct and tells the model what it did.
5. **How are tokens counted?** Provider usage per request; totals are sums over steps; the budget refuses a call
   whose estimate exceeds the remaining input budget.
6. **What with reasoning models?** Their thinking is in `completion_tokens` → counted; prefer non-reasoning models
   for MBPP (1,500 output tokens).
7. **How does the timeout ignore MCP time?** itimer paused in `call_host_tool`; host subtracts `tool_time`.
8. **How do you kill `while True` in C code?** Host hard-kills after timeout + 2 s, restarts the worker, reports reset.
9. **Can sandboxed code read the API key?** No: worker env is `PATH, LANG, PYTHONIOENCODING, PYTHONHASHSEED` only,
   `os` is not importable, MCP servers also get an env without `*API_KEY*`.
10. **Can it escape via `().__class__.__subclasses__()`?** AST rejects dunder attributes; `getattr` too; even if it
    reached `os`, the audit hook blocks the dangerous operations.
11. **Why an audit hook and not only filters?** Filters can be bypassed through objects we did not think of; audit
    events fire inside CPython for the operation itself (`open`, `socket.connect`…) and the hook cannot be removed.
12. **Unknown MCP server?** Tools discovered with `list_tools`, wrappers and manual generated from schemas.
13. **Why `final_answer` is not an MCP tool?** It controls the loop (subject); it is injected by the sandbox and
    always present whatever server is connected.
14. **What happens on `sys.exit()` in the code?** `sys` is not importable; `raise SystemExit` → re-raised by
    `Sandbox.execute` → loop stops → `solution.json` written with the reason.
15. **Rate limit during the exam?** Several keys rotated; backoff; optional fallback provider.
16. **Why mcp 1.x?** 2.x changed the API; 1.x is the stable branch; the protocol is versioned and negotiated.
17. **Why is the first MBPP iteration sometimes only a definition?** The model ignores the one-step pattern; the
    observation tells it "no output — use print()"; the prompt example shows test + `final_answer` in one block.
18. **How do you avoid exceeding 6k input on MBPP?** Compact prompt (~600 tokens), short observations
    (`MBPP_SETTINGS`), one-step pattern; we refuse a call that would exceed.
19. **Where is the configuration?** `sandbox_template.json` (SandboxConfig), `config/models.json` (providers),
    `.env` (keys), CLI flags.
20. **How do you test tools independently?** `LocalExecutor` on a temporary git repo (`tests/test_swebench_tools.py`)
    and over real MCP stdio (`tests/test_mcp.py`).

---

## 6. Live modification drills (2–5 min each, then `git checkout .`)

Rerun after each: `uv run python -m agent_mbpp --task-file cache/mbpp_task.json --output cache/s.json --model-name … --provider-url … --env-file .env`
and show the effect in `cache/s.json` (or `moulinette_eval display`).

| # | Request | Where | How |
|---|---|---|---|
| 1 | Max 3 iterations | `src/agent_mbpp/__main__.py` or CLI | `--max-iterations 3` (no code change) or `limits.max_iterations = 3` in `body()` |
| 2 | Change the system prompt | `agent/prompts.py` `mbpp_system_prompt` | edit the text; show it in `solution.json["system_prompt"]` |
| 3 | Log something per step | `agent/loop.py` `CodeAgent.run`, after `self._record_step(...)` | `print(f"step {step_no}: {chat.input_tokens} in", file=sys.stderr)` |
| 4 | Add a field to each observation (e.g. elapsed time) | `agent/loop.py` `_observation` | `parts.append(f"[time] {self.budget.elapsed():.1f}s")` |
| 5 | Allow an extra import (e.g. `statistics`) | `sandbox_template.json` / `models.py` `SandboxConfig.authorized_imports` | add the name; pass `--sandbox-config sandbox_template.json` |
| 6 | Forbid another builtin (e.g. `print`… or `sorted`) | `sandbox/worker.py` `REMOVED_BUILTINS` | add the name; `uv run sandbox -c "sorted([2,1])"` → NameError |
| 7 | New MCP tool for MBPP | `mcp_tools_mbpp.py` | `@mcp.tool()\n@report\ndef count_lines(code: str) -> str: return str(len(code.splitlines()))` → appears in the manual automatically |
| 8 | Change temperature | `llm/client.py` `LLMClient.__init__(temperature=0.0)` or in `build_client` | `LLMClient(endpoints, temperature=0.2, ...)` |
| 9 | Change the timeout of sandbox code | `SandboxConfig.max_execution_time_seconds` | JSON config, `uv run sandbox cfg.json` then `while True: pass` |
| 10 | Shorter observations | `src/agent_mbpp/__main__.py` `MBPP_SETTINGS.observation_max_chars` | e.g. 500; show `[... observation truncated …]` |
| 11 | Another stop sequence | `extraction/extractor.py` `STOP_SEQUENCES` | max 4 for many providers: replace one |
| 12 | Count retries differently / print them | `llm/client.py` `chat`, `except LLMError` branch | `print("retry", exc, file=sys.stderr)` |
| 13 | Use another model per step | CLI `--model-name`; fallback list `config/models.json` + `--fallback` | — |
| 14 | Make `run_tests` output shorter | `tools/mbpp_tools.py` `run_tests` (report building) | keep only the verdict line |
| 15 | Change read_file page size | `tools/swebench_tools.py` `MAX_READ_LINES` | 400 → 100; call `read_file` on a long file |

Revert: `git checkout . && git clean -fd cache` (keep your `.env`!).

---

## 7. Benchmark report — how to present it
- Setup: ≥5 free models × the same ≥3 SWE-bench tasks (`scripts/run_benchmark.sh`).
- Results table, provider reliability (avg latency, retries, failures), intermediary metrics
  (first touch of a patched file, first decrease of failing tests, steps between first green run and
  `final_answer`) — generated by `scripts/benchmark_table.py` from `benchmarks/**/solution.json`.
- Ablation: same model and tasks, one change (e.g. without the "Method" section of the SWE prompt).
- Conclusions: chosen model(s) and why; which to discard and why — **only from your data**.

---

## 8. Honest limitations (say them before you are asked)
- Some MBPP tasks contradict their hidden test. Example, task 452: "return none if there is no loss", while the
  hidden first test expects `loss_amount(1500,1200)==0`. The agent follows the statement; this is why the threshold is 4/5.
- MBPP `run_tests` sandbox is stricter than the moulinette's Docker Python (allowlist): a solution using a
  non-allowed import fails our tests but could pass theirs (checked: all 425 reference solutions pass ours).
- Token estimate before a call is heuristic (chars/3 with a margin); real counts come from the provider.
- `RLIMIT_AS` only on Linux; on macOS the RSS watchdog kills the worker instead (state is lost, reported).
- `get_patch` only includes tracked files (new files need `git add -N`; the agent is told to keep scratch files in /tmp).
- SWE-bench images are x86_64: on Apple Silicon they are emulated and slow.

## 9. Glossary
CodeAct — tool use through code. MCP — Model Context Protocol (JSON-RPC: tools, resources, prompts).
Stop sequence — string that ends generation. Audit hook — CPython callback for security-relevant events.
RLIMIT_AS — address-space limit. RSS — resident memory. Observation — what the sandbox returns to the LLM.
