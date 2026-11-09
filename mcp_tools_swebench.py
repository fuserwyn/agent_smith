"""MCP server with the mandatory SWE-bench tools (subject V.5).

Run (stdio, default):      python mcp_tools_swebench.py
Run (streamable HTTP):     python mcp_tools_swebench.py --http --port 8001

Environment:
  SWEBENCH_CONTAINER         container name/id -> tools act via `docker exec`
                             (unset -> tools act on the local filesystem)
  TESTBED_PATH               repository root (default /testbed)
  SWEBENCH_EVAL_SCRIPT_FILE  host path of the task's eval script (used by run_tests;
                             falls back to /eval.sh inside the container)
"""

from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path
from typing import Optional

sys.path.insert(0, str(Path(__file__).resolve().parent / "src"))

from mcp.server.fastmcp import FastMCP  # noqa: E402

from agent_smith.tools.executor import executor_from_env  # noqa: E402
from agent_smith.tools.instrument import instrumented  # noqa: E402
from agent_smith.tools.swebench_tools import SWEBenchTools  # noqa: E402


def _load_tools() -> SWEBenchTools:
    script = None
    path = os.environ.get("SWEBENCH_EVAL_SCRIPT_FILE")
    if path and os.path.isfile(path):
        script = Path(path).read_text(encoding="utf-8")
    return SWEBenchTools(executor_from_env(), eval_script=script)


TOOLS = _load_tools()
mcp = FastMCP("agent-smith-swebench",
              instructions=f"Tools to explore and fix the repository at {TOOLS.ex.root}.", log_level="WARNING")
# Each result carries an execution report in _meta (commands run in the repository, timings, sizes).
def _reset() -> None:
    TOOLS.ex.reset_ops()
    TOOLS.details = {}


report = instrumented("agent-smith-swebench", "SWEBenchTools", lambda: TOOLS.ex.ops, _reset,
                      lambda: {"executor": TOOLS.ex.kind, "target": TOOLS.ex.target, "repo_root": TOOLS.ex.root},
                      lambda: dict(TOOLS.details))


@mcp.tool()
@report
def read_file(filepath: str, start_line: int = 1, end_line: Optional[int] = None) -> str:
    """Read a file with line numbers ('<n>: <line>'). Relative paths are from the repo root.
    Reads at most 400 lines per call; use start_line/end_line to page."""
    return TOOLS.read_file(filepath, start_line, end_line)


@mcp.tool()
@report
def edit_file(filepath: str, old_str: str, new_str: str) -> str:
    """Replace the exact string old_str (must occur exactly once, whitespace-sensitive) with new_str.
    The edit is rejected if it would introduce a Python syntax error. Empty old_str on a missing file creates it."""
    return TOOLS.edit_file(filepath, old_str, new_str)


@mcp.tool()
@report
def list_files(directory: str = ".", pattern: str = "*") -> str:
    """List files under a directory whose name matches a glob pattern (e.g. '*.py', 'test_*.py')."""
    return TOOLS.list_files(directory, pattern)


@mcp.tool()
@report
def search_code(pattern: str, file_pattern: str = "*.py") -> str:
    """grep -E like search over the repository. Output: '/abs/path.py:<line> <content>'."""
    return TOOLS.search_code(pattern, file_pattern)


@mcp.tool()
@report
def search_function_or_class_definition_in_code(name: str) -> str:
    """Find where a function or class is defined (def/class). Output like search_code."""
    return TOOLS.search_function_or_class_definition_in_code(name)


@mcp.tool()
@report
def find_references(name: str, filepath: Optional[str] = None, line: Optional[int] = None) -> str:
    """Find all usages of a symbol (whole-word match). Matches in `filepath` near `line` are listed first."""
    return TOOLS.find_references(name, filepath, line)


@mcp.tool()
@report
def run_tests() -> str:
    """Run the task's evaluation script; returns a pass/fail summary plus the tail of the test output."""
    return TOOLS.run_tests()


@mcp.tool()
@report
def get_patch() -> str:
    """Return the unified diff of all changes (git -c core.fileMode=false diff)."""
    return TOOLS.get_patch()


@mcp.tool()
@report
def run_command(command: str, workdir: Optional[str] = None, timeout: int = 120) -> str:
    """Run a shell command (bash) in workdir (default: repo root). Returns exit code, stdout and stderr."""
    return TOOLS.run_command(command, workdir, timeout)


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--http", action="store_true", help="serve streamable HTTP instead of stdio")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8001)
    args = parser.parse_args()
    if args.http:
        mcp.settings.host, mcp.settings.port = args.host, args.port
        mcp.run(transport="streamable-http")
    else:
        mcp.run()


if __name__ == "__main__":
    main()
