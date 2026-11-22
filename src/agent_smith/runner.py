"""Shared CLI plumbing for the two benchmark agents (MBPP and SWE-bench).

Guarantees that a valid ``solution.json`` is written even if something crashes
(errors are handled gracefully, the error message goes into ``error``).
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import signal
import sys
import time
from pathlib import Path
from typing import Callable, Dict, Optional, Tuple

from agent_smith.codelog import CodeLog, code_root
from agent_smith.config import PROJECT_ROOT, load_env_file, load_models_config
from agent_smith.events import EventSink
from agent_smith.models import SolutionOutput


class InputError(Exception):
    """Bad input (task file, model choice, keys): reported in ``error`` with one line, no traceback."""


def common_parser(prog: str, description: str) -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog=prog, description=description)
    p.add_argument("--task-file", required=True, help="task JSON dumped by the moulinette")
    p.add_argument("--output", required=True, help="where to write solution.json")
    p.add_argument("--model-name", help='e.g. "qwen/qwen3.8-27b" (default: the first free model of '
                   '`benchmark_defaults` in config/models.json whose provider has a key in the environment)')
    p.add_argument("--provider-url", help='e.g. "https://api.groq.com/openai/v1" (default: see --model-name)')
    p.add_argument("--env-file", help="load API keys from this .env file")
    p.add_argument("--sandbox-config", help="SandboxConfig JSON (default: built-in defaults)")
    p.add_argument("--max-iterations", type=int, help="override max_iterations (cannot exceed the hard limit)")
    p.add_argument("--fallback", action="store_true", help="fall back to providers listed in config/models.json")
    p.add_argument("--mcp-stdio", help="use this MCP server command instead of the default one")
    p.add_argument("--mcp-server", help="use a streamable HTTP MCP server instead of the default one")
    p.add_argument("--trace-file", default=os.environ.get("AGENT_TRACE_FILE"),
                   help="write a JSONL event trace (for the UI: uv run agent-ui --trace FILE)")
    p.add_argument("-v", "--verbose", action="store_true")
    return p


def setup(args: argparse.Namespace) -> EventSink:
    logging.basicConfig(level=logging.INFO if args.verbose else logging.WARNING, stream=sys.stderr,
                        format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    load_env_file(args.env_file, override=bool(args.env_file))
    # SIGTERM (e.g. exam timeout) -> SystemExit so `finally` blocks clean up containers.
    signal.signal(signal.SIGTERM, lambda *_: sys.exit(143))
    # opt-in (AGENT_SMITH_CODE_DIR): <dir>/runs/<benchmark>/<run>/step_NN.py, the code each step sent to the worker
    return EventSink(args.trace_file, listeners=[CodeLog(code_root())])


def pick_model(args: argparse.Namespace, benchmark: str) -> Tuple[str, str]:
    """``--model-name`` / ``--provider-url``, or the free default of this benchmark for the keys in the environment."""
    if args.model_name and args.provider_url:
        return args.model_name, args.provider_url
    cfg = load_models_config()
    if args.provider_url:  # a provider without a model: its first default
        spec = cfg.provider_for_url(args.provider_url)
        entry = next((e for e in cfg.benchmark_defaults.get(benchmark) or []
                      if spec and e.get("provider") == spec.name), None)
        if entry:
            return entry["model"], args.provider_url
        raise InputError(f"--provider-url {args.provider_url} needs --model-name (no default model for it)")
    if args.model_name:  # a model without a provider: the provider listing it in config/models.json
        spec = next((cfg.provider_by_name(m.provider) for m in cfg.models if m.model == args.model_name), None)
        if spec:
            return args.model_name, spec.base_url
        raise InputError(f"--model-name {args.model_name} needs --provider-url (model not in config/models.json)")
    picked = cfg.default_model(benchmark)
    if picked is None:
        envs = sorted({cfg.provider_by_name(e["provider"]).key_env for e in cfg.benchmark_defaults.get(benchmark) or []
                       if cfg.provider_by_name(e["provider"])} - {None})
        raise InputError("no API key found for any default model: set one of " + ", ".join(envs)
                         + " in the environment or .env file, or pass --model-name and --provider-url")
    logging.getLogger("agent").info("default model for %s: %s @ %s", benchmark, *picked)
    return picked


def read_task(path: str) -> Tuple[Dict, Optional[str]]:
    """The task JSON, or ({}, error message): a missing or broken file must not crash the CLI."""
    try:
        with open(path, encoding="utf-8") as fh:
            data = json.load(fh)
        if not isinstance(data, dict):
            return {}, f"task file {path} does not contain a JSON object"
        return data, None
    except FileNotFoundError:
        return {}, f"task file not found: {path}"
    except (OSError, ValueError) as exc:
        return {}, f"cannot read task file {path}: {exc}"


def server_env(extra: Dict[str, str]) -> Dict[str, str]:
    """Environment for MCP servers: inherit PATH etc. but never the API keys."""
    env = {k: v for k, v in os.environ.items() if "API_KEY" not in k and "TOKEN" not in k.upper()}
    env.update(extra)
    env["PYTHONPATH"] = str(PROJECT_ROOT / "src")
    return env


def default_server(name: str) -> str:
    return f'"{sys.executable}" "{PROJECT_ROOT / name}"'


def write_solution(path: str, output: SolutionOutput) -> None:
    Path(path).parent.mkdir(parents=True, exist_ok=True)
    Path(path).write_text(output.model_dump_json(indent=2), encoding="utf-8")


def run_cli(benchmark: str, task_id: str, args: argparse.Namespace, started: float,
            body: Callable[[], SolutionOutput], events: Optional[EventSink] = None) -> int:
    """Run ``body`` and always write a solution file."""
    exit_code = 0
    try:
        output = body()
    except KeyboardInterrupt:
        output = _failure(benchmark, task_id, started, "interrupted (KeyboardInterrupt)")
        exit_code = 130
    except SystemExit as exc:
        output = _failure(benchmark, task_id, started, f"terminated (SystemExit {exc.code})")
        exit_code = exc.code if isinstance(exc.code, int) else 1
    except InputError as exc:
        print(f"error: {exc}", file=sys.stderr)
        output = _failure(benchmark, task_id, started, str(exc))
        exit_code = 2
    except Exception as exc:  # noqa: BLE001 - must never crash without an output file
        logging.getLogger("agent").exception("agent failed")
        output = _failure(benchmark, task_id, started, f"{type(exc).__name__}: {exc}")
        exit_code = 1
    write_solution(args.output, output)
    if events:
        events.close()
    status = "SUCCESS" if output.success else "FAILED"
    print(f"[{benchmark}] task {task_id}: {status} | iterations={output.iterations} "
          f"in={output.total_input_tokens} out={output.total_output_tokens} "
          f"time={output.total_time_seconds:.1f}s -> {args.output}" + (f" | {output.error}" if output.error else ""))
    return exit_code


def _failure(benchmark: str, task_id: str, started: float, error: str) -> SolutionOutput:
    return SolutionOutput(task_id=str(task_id), benchmark=benchmark, success=False, solution="", iterations=0,
                          total_requests=0, total_input_tokens=0, total_output_tokens=0,
                          total_time_seconds=round(time.monotonic() - started, 3), error=error)
