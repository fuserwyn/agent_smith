"""Shared CLI plumbing for ``agent_mbpp`` and ``agent_swebench``.

Guarantees that a valid ``solution.json`` is written even if something crashes
(errors are handled gracefully, the error message goes into ``error``).
"""

from __future__ import annotations

import argparse
import logging
import os
import signal
import sys
import time
from pathlib import Path
from typing import Callable, Dict, Optional

from agent_smith.config import PROJECT_ROOT, load_env_file
from agent_smith.events import EventSink
from agent_smith.models import SolutionOutput


def common_parser(prog: str, description: str) -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog=prog, description=description)
    p.add_argument("--task-file", required=True, help="task JSON dumped by the moulinette")
    p.add_argument("--output", required=True, help="where to write solution.json")
    p.add_argument("--model-name", required=True, help='e.g. "qwen/qwen3-coder:free"')
    p.add_argument("--provider-url", required=True, help='e.g. "https://openrouter.ai/api/v1"')
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
    return EventSink(args.trace_file)


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
