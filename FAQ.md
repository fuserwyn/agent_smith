# Agent Smith — FAQ

[Русская версия](FAQ.ru.md) · [README](README.md)

---

## In short

### What is it?
Two things in one repository:

1. **Agent Smith** — a code-agent framework written from scratch (no smolagents, LangGraph, LlamaIndex, CrewAI,
   AutoGen). The model reasons, writes **Python code that calls tools**, the code runs in an **isolated
   sandbox**, the result goes back to the model — until `final_answer(...)`. It solves MBPP tasks and fixes real
   SWE-bench Verified bugs inside Docker containers.
2. **Agent Smith Studio** — a local web IDE around the agent: explorer, Monaco editor, real terminal, git, and a
   chat where **every step of the agent is visible and every change can be reviewed and undone**, plus "Inside
   the agent", which plays the agent's work on its architecture diagram — live.

### How is this different from "yet another LLM chat"?
- The agent **acts with code**, not JSON calls: in one step it can read a file, find the spot, edit it and check
  it — in plain Python.
- Everything it does is **transparent**: open any step to see the thought, the exact code, each executed line,
  each tool call and what the model returned.
- **Safety is in the architecture**, not in the prompt: the model's code physically cannot open the network, read
  the keys or leave the allowed folders.
- It runs **for free**: free models by default; a paid model is available and clearly marked.

### How do I try it in a minute?
```bash
git checkout agent-smith-studio
make studio-install                 # once
make studio P=~/path/to/project     # opens http://127.0.0.1:8790/?token=…
```
Provider keys go in Agent Smith's own `.env` (see `.env.example`). The terminal needs Docker running (or
`--shell local`).

---

## The agent core

### How does the loop work?
`Thought → Code → Observation → … → final_answer`. Before every model call the **budget** is checked (iterations,
input and output tokens, time): the loop never makes a call that could break a limit. When the budget runs out,
the agent submits its best candidate (MBPP: the last code that passed the tests; SWE-bench: the current patch),
honestly marked `success=false` with the reason.

### What if the model answers in the wrong format?
Code extraction understands five formats — a ```` ```python ```` block, `<code>`, XML invocations, Hermes JSON and
ReAct — and turns them into Python. With no code, the model gets an explicit message with the expected format. If
the code had to be repaired, the observation says so: nothing happens silently.

### How does the agent know its tools?
They are **not hard-coded**: they come from the MCP server at connection time (`list_tools`), become Python
functions with proper arguments, and the instructions for the model are generated from their schemas. So the agent
works with any MCP server, not only its own. stdio and streamable HTTP, plus MCP resources and prompts.

### How does it avoid going in circles?
If the model sends exactly the code of a recent step, it is **not run again**: the model gets a `[LOOP]` note with
a hint about what to do next, and the temperature rises a little to break the loop.

### How good is it?
From the SWE-bench Verified report (every number generated from real `solution.json` files validated by the
moulinette): `nvidia/nemotron-3.5-lightning-30b-a3b` solved **3 of 3** tasks, 146 s for all three, on a free tier.
MBPP: 5/5 in the mock exam with two free models. Details, a 5-model comparison and a prompt ablation are in
[BENCHMARK_REPORT.md](BENCHMARK_REPORT.md).

---

## The sandbox

### What stops the model's code from doing something bad?
It runs in a **separate process** (`python -I -S`, empty environment, no API keys) behind several layers:

| Layer | What it blocks |
|---|---|
| AST policy | dunder attributes (`__class__`, `__subclasses__`, `__globals__`…), frame attributes, imports outside the allowlist |
| Restricted builtins | no `eval`, `exec`, `compile`, `globals`…; `getattr`/`setattr` refuse dunder names |
| Module proxies | an imported module is a read-only view without private names or non-allowlisted sub-modules |
| Audit hook | files outside the allowed folders, sockets (network), `subprocess`, `os.system/fork/kill`, `ctypes` — and it cannot be removed |
| Timeout | per code block; cannot be swallowed by `except Exception`; paused during tool calls |
| Memory | process limit; the host watches usage and restarts the worker |

The tools (reading files, edits, tests) live **outside** the sandbox, in the MCP server: the worker only asks for
them through a proxy.

### And SWE-bench?
Each task runs in its own Docker container, `--network none`, labelled, and always removed — even on Ctrl+C or a
crash.

---

## The observatory ("Inside the agent")

### What does it show?
The architecture diagram (task → orchestrator → LLM → code extraction → sandbox → MCP client → MCP server →
repository) with the run's events flowing through it. Click any block to see what went in and out at the selected
step; follow the code **line by line** (which line ran, with which values); see tool calls, JSON-RPC, the budget,
the SWE-bench test harness.

### Live?
Yes. In the studio the diagram **plays the events as they arrive**, a little behind the agent, speeding up (up to
8×) when it falls behind. While a run is going the counter reads "event 9 of 10+ · running". A finished run opens on
its final state; ⏮ replays it.

### One diagram for everything?
No, it adapts: MBPP shows the test sandbox, SWE-bench the repository in its container, a studio turn your project
(in the studio's container or a local folder).

### Without the studio?
`ui/index.html` works on its own too: recorded demos, trace files, real runs launched with a button. Three
languages (EN/RU/FR), light and dark themes, a guided tour.

---

## The studio (mini-IDE)

### What can it do as an IDE?
- Explorer with live file-system updates, tabs, single-click preview.
- **Monaco** (VS Code's editor): highlighting, saving with conflict detection — if the file changed on disk,
  compare, overwrite or reload.
- Quick open (⌘P) with fuzzy matching, search in files (⌘⇧F), git status and diffs.
- A **real terminal** (xterm.js) — several tabs, survives a page reload.
- Command palette (⌘K), settings, welcome screen, EN/RU/FR, light and dark themes.
- Shortcuts work even when the terminal has the focus (like VS Code); Cmd on macOS, Ctrl elsewhere.

### Why is the terminal in Docker?
By default each tab is a `docker exec` into the project's container: only the project folder is mounted, processes
run as your user, the rest of the machine is out of reach. `--shell local` (a normal shell) exists but must be
asked for and is flagged in the status bar. Variables holding keys and tokens are removed from every shell's
environment.

---

## The agent in the studio

### Ask vs Agent?
- **Ask** — read only: the tools that change things are not even offered to the model.
- **Agent** — edits files and runs commands. Before each turn the studio takes a **checkpoint** in a shadow git
  repository (outside the project — your `.git` is never touched). After the turn you see which files changed,
  each with a diff, and can **undo one file or the whole turn**.

### Can I stop it midway?
Yes: Stop takes effect before the next code block runs, and also stops a benchmark run in progress.

### Can I mention a file?
`@` in the message box searches the project's files.

---

## Running benchmarks from the chat

### How do I run MBPP or SWE-bench "with a button"?
With a chat command — no chat model, no tokens:
```
/mbpp 2          /mbpp           /swe            /swe sympy__sympy-13480 <model>
```
It starts the real pipeline: task from the moulinette → `agent_mbpp` / `agent_swebench` → validation. The chat shows
a card with the stages and the verdict, and "Inside the agent" switches to the inner agent and shows it working,
live. Type `/` for a hint.

### And in plain words?
"Run one MBPP test" works too: the agent has a `run_agent_smith` tool, which it calls in its first step. One such
run per turn; unknown task ids are refused at once with the nearest valid ones. Checked with a free model: 17 s,
816 tokens.

---

## The agent builds and runs apps

### Can I ask "write a server and run it"?
Yes. For example: *"Make a simple FastAPI server with / and /health, in Docker, and run it on port 8123"*. The agent
writes the whole project in `agent_code/apps/<name>/` (code, Dockerfile, dependencies) and calls `run_app`: the
studio builds the image and runs the container on a free (or requested) port on `127.0.0.1`, waits until it
answers — then the agent checks it with `call_app` and gives you the URL. Both such requests were checked live with
a free model.

### Where do I see what the app is doing?
A card in the chat (URL, state, logs, stop), the "Apps" tab at the bottom (list and live logs), a counter in the
status bar.

### Is it safe?
The container listens on the loopback address only and is limited in memory (512 MB), CPU and processes, with no
capabilities and no privilege escalation. `call_app` only reaches apps the studio started. Stopping it, or closing
the studio, removes the containers; the images are removed on exit.

---

## Generated code (`agent_code/`)

### Where can I see the code the agent actually ran?
Every run — from the terminal, in the exam, in the studio — keeps it in
`agent_code/runs/<benchmark>/<date>_<task>/step_NN.py`: the thought as a header, **exactly the code sent to the
worker**, and its result as a trailing comment, plus `task.md` with the prompt. Handy to study a run, re-run a step
by hand or diff two runs. The folder ignores itself in git (a `.gitignore` with `*` inside);
`AGENT_SMITH_CODE_DIR=off` disables it.

---

## Studio security

### The studio exposes my files and a terminal — isn't that dangerous?
It is built for one person on their own machine and protected like Jupyter, only stricter:
- listens on **loopback only** (`127.0.0.1`);
- a **session token** in the first URL; `Referrer-Policy: no-referrer` so it cannot leak;
- `Host` check (against DNS rebinding) and `Origin` check, an `X-Studio` header on state-changing requests (CSRF);
- a strict **Content-Security-Policy**: only its own scripts, no third-party connections; an e2e test fails on any
  CSP violation;
- a **file jail**: no `..`, no absolute paths, no escape through a symlink; atomic writes that keep permissions;
- `.env`-like files open only after an explicit confirmation;
- **keys never reach the browser**, the shells or the MCP servers; only Agent Smith's own `.env` is read, never the
  project's.

---

## Models and cost

### Is it free?
By default, yes: free tiers of NVIDIA, Google AI Studio, Groq, Cohere, OpenRouter. Several keys per provider are
rotated, with retries and fallback to backup models. A paid OpenRouter model is available, marked `$`.

### Which models can I pick?
Any OpenAI-compatible model in `config/models.json` (your own list via `AGENT_SMITH_MODELS_FILE`). In the studio,
models without a key are shown but disabled, and Settings tells you which variable to add.

---

## Quality

### How is it tested?
- **175 Python tests**: agent loop, extraction, sandbox, MCP, tools, studio API, file jail, terminal, checkpoints,
  benchmark runs, apps (with a fake Docker that serves real HTTP).
- **19 Playwright scenarios** drive the real studio server in a browser with a scripted model: editor, terminal,
  agent with diff and undo, "Inside the agent", command palette, CSP.
- Tests against real Docker behind `STUDIO_DOCKER_TESTS=1`.
- CI on every push: Python, web build, e2e. GitHub Actions pinned by SHA, dependencies at least two weeks old.

### Were the bugs found along the way fixed?
Yes, each with a test that reproduces it: a race where a turn was "done" before its changes were computed, the
last events lost in the live diagram, the chat column overflowing, the terminal stealing the focus, a model
repeating an empty code block up to the token limit, and more — see the commit history.

### Why GIFs rather than screenshots in the README?
They are recorded automatically: `make studio-gif` drives the real studio with a scripted model and a visible
pointer, and the frames are assembled into GIFs. After a UI change they are re-recorded with one command.

---

## Honest limitations

- The studio is local and single-user; one agent turn runs at a time.
- The default terminal and the apps need Docker.
- SWE-bench images are x86_64; on Apple Silicon they run under emulation and are slower.
- Free tiers have quotas (e.g. about 20 requests a day for some Gemini models).
- Small models sometimes wander; the studio limits that with the turn budget, stop sequences and tool rules, but
  the answer's quality still depends on the model.
