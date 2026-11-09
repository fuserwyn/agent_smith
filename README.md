*This project has been created as part of the 42 curriculum by johnytaker.*

# Agent Smith

## Description

Agent Smith is a code-agent framework written from scratch (no smolagents / LangGraph / LlamaIndex / CrewAI / AutoGen).
An LLM reasons about a task, writes **Python code that calls tools**, the code runs in a **restricted sandbox**,
the output is fed back, and the loop repeats until the agent calls `final_answer(...)`:

```
Thought → Code → Observation → … → final_answer
```

It is applied to two benchmarks:

| Benchmark | Goal | Limits per task (cumulative) |
|---|---|---|
| MBPP | write a correct Python function | 10 iterations · 6k input / 1.5k output tokens · 120 s |
| SWE-bench Verified | produce a git patch fixing a real issue in a Docker image | 30 iterations · 300k / 10k tokens · 900 s |

## Instructions

Requirements: Python 3.10, [uv](https://docs.astral.sh/uv/), Docker (SWE-bench only).

```bash
uv sync                          # install
cp .env.example .env             # add your free-tier API keys (several per provider are rotated)
uv run pytest                    # offline test-suite (no API key, no Docker needed)
```

**Sandbox**

```bash
uv run sandbox                                                      # interactive REPL (exit / Ctrl+D)
uv run sandbox sandbox_template.json                                # custom SandboxConfig
uv run sandbox --mcp-stdio "python mcp_tools_mbpp.py" sandbox_template.json
uv run sandbox --mcp-server http://127.0.0.1:8000/mcp               # streamable HTTP
uv run sandbox --mcp-stdio "python mcp_tools_swebench.py" sandbox_template.json
uv run sandbox --mcp-stdio "python mcp_tools_mbpp.py" --manual      # print the generated tool manual
```

An MCP server can be started over HTTP with `python mcp_tools_mbpp.py --http --port 8000`.

**Agents** (same interface for both; keys come from the environment or `--env-file`)

```bash
cd moulinette && uv run moulinette_eval dump mbpp --output ../cache/mbpp_task.json && cd ..
uv run python -m agent_mbpp --task-file cache/mbpp_task.json --output cache/mbpp_solution.json \
    --model-name "qwen/qwen3-coder:free" --provider-url "https://openrouter.ai/api/v1"
cd moulinette && uv run moulinette_eval validate mbpp ../cache/mbpp_task.json ../cache/mbpp_solution.json

uv run python -m agent_swebench --task-file cache/swebench_task.json --output cache/swebench_solution.json \
    --model-name "qwen/qwen3-coder:free" --provider-url "https://openrouter.ai/api/v1"
```

Useful options: `--env-file PATH`, `--trace-file runs/live.jsonl` (event trace for the UI), `--fallback`
(fall back to providers listed in `config/models.json`), `--max-iterations N`, `--mcp-stdio CMD` / `--mcp-server URL`
(use another MCP server), `-v`.

**Interactive UI**: `ui/index.html` replays a run on the architecture diagram (active component, step timeline,
budget, LLM output, sandbox input, tool calls, observations). Run `uv run agent-ui --trace runs/live.jsonl`, open
http://127.0.0.1:8765 and start an agent with `--trace-file runs/live.jsonl` to watch it live. You can also drop any
`events.jsonl` or `solution.json` on the page.

API keys: `<PROVIDER>_API_KEY`, `<PROVIDER>_API_KEY_2…_9` or `<PROVIDER>_API_KEYS=k1,k2`
(`OPENROUTER`, `GROQ`, `GEMINI`, `MISTRAL`, `TOGETHER`, `CEREBRAS`, `NVIDIA`; `LLM_API_KEY` for any other
OpenAI-compatible URL). Keys are never written in code or config.

## System architecture

```
 task.json ─▶ agent_mbpp / agent_swebench (CLI, limits, solution.json)
                    │
                    ▼
            Orchestrator (agent/loop.py) ◀──────────────▶ LLM API (llm/: providers, key pool, retries, fallback)
                    │ raw response
                    ▼
            Code extraction (extraction/)  python block · <code> · XML invoke · Hermes JSON · ReAct → Python
                    │ python code
   ┌────────────────▼──────────── Sandbox ─────────────────────────┐
   │  worker process (sandbox/worker.py)      host (sandbox/host.py)│
   │  restricted namespace ── tool_call ──▶   MCP client (mcp_client/) ──stdio/HTTP──▶ MCP server
   │  final_answer() built in  ◀─ result ──                          │      (mcp_tools_*.py, tools/)
   └────────────────────────────────────────────────────────────────┘                │
                                                                         docker exec ▼
                                                                     task container /testbed
```

| Path | Responsibility |
|---|---|
| `src/agent_smith/models.py` | Pydantic models from the subject (`SandboxConfig`, task inputs, `StepMetrics`, `SolutionOutput`) and limits |
| `src/agent_smith/agent/` | loop, cumulative budget, prompts |
| `src/agent_smith/llm/` | OpenAI-compatible provider, key rotation, retries, fallback, usage |
| `src/agent_smith/extraction/` | multi-format code extraction with explicit repair notes |
| `src/agent_smith/sandbox/` | worker process, host proxy, generated manual, REPL CLI |
| `src/agent_smith/mcp_client/` | synchronous MCP client (stdio + streamable HTTP), dynamic discovery |
| `src/agent_smith/tools/` | tool implementations (independent of MCP and of the loop) |
| `mcp_tools_mbpp.py`, `mcp_tools_swebench.py` | MCP servers (FastMCP) |
| `src/agent_smith/docker_env.py` | container lifecycle (`--network none`, labelled, always removed) |
| `ui/`, `src/agent_smith/ui_server.py`, `events.py` | event trace and visualiser |

## Agent loop

1. Build messages: system prompt (format rules + **manual generated from the MCP tool schemas** + method), task,
   then the history of `(assistant, Observation)` pairs. Older observations are shortened to save tokens.
2. **Budget check** before every call: estimated input tokens must fit the remaining cumulative budget; `max_tokens`
   is capped by the remaining output budget; the HTTP timeout by the remaining time. The loop never makes a call
   that could break a limit.
3. Call the LLM with stop sequences (`<end_code>`, `\nObservation:`, `</tool_call>`, `</function_calls>`) so it
   cannot invent observations.
4. Extract code; if none, the LLM gets an explicit "no valid code block" message with the expected format.
5. Execute in the sandbox; the observation contains stdout, the last expression value, errors with line numbers
   and explicit notes (repaired code, timeout with partial output, truncated output, sandbox reset, repeated code).
6. `final_answer(x)` ends the run. If the budget runs out first, the best candidate is submitted
   (MBPP: last code that passed `run_tests`; SWE-bench: current `get_patch()`), with `success=false` and the reason.
7. Each step is stored as `StepMetrics` (`llm_output`, `sandbox_input`, `sandbox_output`, tokens, latency, retries,
   model, URL); `SolutionOutput` adds totals and the full system prompt.

## Sandbox design

Untrusted code runs in a **separate process** (`python -I -S worker.py`, empty environment, own session).
Communication is JSON lines over private file descriptors; fd 0/1 point to `/dev/null` so code cannot forge messages.

| Layer | What it blocks |
|---|---|
| AST policy | dunder attributes (`__class__`, `__subclasses__`, `__globals__`…), frame attributes, imports outside the allowlist |
| Restricted builtins | no `eval`, `exec`, `compile`, `input`, `globals`, `vars`, `breakpoint`…; `getattr`/`setattr` refuse dunder names; `__import__` enforces `authorized_imports` (`math.*` = sub-modules) |
| Module proxies | imported modules are read-only views hiding `_private` names and non-allowlisted sub-modules (`random._os`, `typing.sys`) |
| Audit hook (`sys.addaudithook`) | `open`/`os.*` outside `allowed_directories`, raw fds, sockets (network), `subprocess`, `os.system/exec/fork/kill`, `ctypes` — cannot be removed once installed |
| Timeout | `SIGALRM` per code block, raised as a `BaseException` (cannot be swallowed by `except Exception`), **paused during MCP tool calls**; the host hard-kills a worker stuck in C code and restarts it |
| Memory | `RLIMIT_AS` on Linux; on every OS the host watches the worker's RSS (also the peak reported after each block) and kills/restarts it |
| Output | streamed to the host, capped with an explicit truncation note; partial output survives timeouts |

`SystemExit` and `KeyboardInterrupt` raised by sandboxed code are re-raised by `Sandbox.execute()` so they reach the
agent loop, which stops cleanly and still writes `solution.json`. `final_answer` is a sandbox built-in, not an MCP tool;
it also accepts a function object (its source is recovered). Everything is configured with `SandboxConfig` (JSON).

**Isolation choice.** In-process restriction would make timeouts and memory limits unreliable (no way to stop a
C-level loop) and keep the agent's secrets in reach. A child process gives a hard kill, OS limits and an empty
environment; the cost is the tool-call proxy, which is also what lets MCP tools stay outside the security boundary.

## Tool implementation details

MCP tools are discovered at runtime (`list_tools`) and injected as Python functions with positional/keyword
argument mapping from the JSON schema; resources and prompts are exposed as `list_resources()/read_resource(uri)`
and `list_prompts()/get_prompt(name, arguments)`. Nothing is hard-coded, so an unknown MCP server works the same way.

**MBPP** (`mcp_tools_mbpp.py`): `run_tests(code, tests=None)` runs the candidate *inside a sandbox* and reports
each assertion with got/expected values. Task context comes from `MBPP_TASK_FILE`; resource `mbpp://task`.

**SWE-bench** (`mcp_tools_swebench.py`, logic in `tools/swebench_tools.py`): act through `docker exec` when
`SWEBENCH_CONTAINER` is set, otherwise on the local disk under `TESTBED_PATH` (used for independent tests).

| Tool | Notes |
|---|---|
| `read_file(filepath, start_line, end_line)` | `N: line`, 400 lines max per call with a paging hint |
| `edit_file(filepath, old_str, new_str)` | exact, unique match; shows similar lines when not found and line numbers when ambiguous; **rejects edits that introduce a syntax error**; drops stale `.pyc` |
| `list_files(directory, pattern)` | `find`, skips `.git`/caches, capped |
| `search_code(pattern, file_pattern)` | `/abs/path.py:N content`; invalid regex falls back to a literal search |
| `search_function_or_class_definition_in_code(name)` | `def`/`class` (and assignments as fallback) |
| `find_references(name, filepath, line)` | whole-word matches, the given file first |
| `run_tests()` | task eval script (or `/eval.sh`), pass/fail summary + output tail |
| `get_patch()` | `git -c core.fileMode=false diff` |
| `run_command(command, workdir)` | exit code, stdout, stderr (with timeout) |

## Benchmark results and analysis

See [BENCHMARK_REPORT.md](BENCHMARK_REPORT.md). Runs are produced by `scripts/run_benchmark.sh` and the tables by
`scripts/benchmark_table.py`, only from real `solution.json` files kept in `benchmarks/`.

## Resources

- Model Context Protocol specification and Python SDK — https://modelcontextprotocol.io
- MBPP dataset (Austin et al., 2021, *Program Synthesis with Large Language Models*)
- SWE-bench (Jimenez et al., 2024) and SWE-bench Verified — https://www.swebench.com
- *Executable Code Actions Elicit Better LLM Agents* (CodeAct, Wang et al., 2024)
- *ReAct: Synergizing Reasoning and Acting in Language Models* (Yao et al., 2023)
- SWE-agent (agent-computer interfaces) and mini-SWE-agent, read as references for minimal agent design
- Python docs: `sys.addaudithook` / audit events table, `resource`, `signal.setitimer`, `ast`, `codeop`

**Use of AI.** An AI assistant (Claude) was used to review the subject against the project plan, to draft the
initial implementation of the modules listed above, the test-suite and the visualiser, and to draft this README.
All design decisions (process isolation, audit hook, budget policy, tool output formats) were reviewed and must
be understood and defended by the author; the tests in `tests/` document the expected behaviour of each part.
