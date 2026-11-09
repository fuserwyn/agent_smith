"""MCP server with MBPP tools.

Run (stdio, default):      python mcp_tools_mbpp.py
Run (streamable HTTP):     python mcp_tools_mbpp.py --http --port 8000   -> http://127.0.0.1:8000/mcp

Task context comes from the environment: ``MBPP_TASK_FILE`` = path to the task
JSON dumped by the moulinette. Without it, ``run_tests`` needs explicit tests.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path
from typing import List, Optional

sys.path.insert(0, str(Path(__file__).resolve().parent / "src"))

from mcp.server.fastmcp import FastMCP  # noqa: E402

from agent_smith.tools import mbpp_tools  # noqa: E402
from agent_smith.tools.instrument import instrumented  # noqa: E402

mcp = FastMCP("agent-smith-mbpp", instructions="Tools to test Python solutions for MBPP tasks.",
              log_level="WARNING")
report = instrumented("agent-smith-mbpp", "mbpp_tools", lambda: mbpp_tools.OPS, mbpp_tools.OPS.clear,
                      lambda: {"executor": "sandbox", "target": "isolated sandbox worker (no MCP, no network)"},
                      lambda: dict(mbpp_tools.DETAILS))


@mcp.tool()
@report
def run_tests(code: str, tests: Optional[List[str]] = None) -> str:
    """Run the task's test assertions against a candidate solution.

    `code` is the COMPLETE solution source (needed imports + the function), as a string.
    `tests` is optional: by default the task's public tests are used.
    Returns 'ALL TESTS PASSED (n/n)' or 'TESTS FAILED (k/n passed)' with got/expected values.
    """
    return mbpp_tools.run_tests(code, tests)


@mcp.resource("mbpp://task", description="The current MBPP task (JSON)")
def task_resource() -> str:
    task = mbpp_tools.load_task_from_env()
    return task.model_dump_json(indent=2) if task else "{}"


@mcp.prompt(description="Instructions for solving an MBPP task")
def solve_mbpp(task_definition: str, function_definition: str) -> str:
    return (f"Task: {task_definition}\nImplement: {function_definition}\n"
            "Write the function, verify it with run_tests(code), then call final_answer(code).")


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--http", action="store_true", help="serve streamable HTTP instead of stdio")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8000)
    args = parser.parse_args()
    if args.http:
        mcp.settings.host, mcp.settings.port = args.host, args.port
        mcp.run(transport="streamable-http")
    else:
        mcp.run()


if __name__ == "__main__":
    main()
