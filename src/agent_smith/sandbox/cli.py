"""``uv run sandbox`` — interactive sandbox REPL.

Examples::

    uv run sandbox
    uv run sandbox sandbox_template.json
    uv run sandbox --mcp-stdio "python mcp_tools_mbpp.py" sandbox_template.json
    uv run sandbox --mcp-server http://127.0.0.1:8000/mcp
    uv run sandbox -c "print(1 + 1)"          # one-shot, non-interactive

Each entry runs inside the same restricted namespace as the agent's code
(imports, filesystem, timeout and memory limits apply) with the MCP tool
wrappers and ``final_answer`` available. ``exit`` or EOF (Ctrl+D) quits.
"""

from __future__ import annotations

import argparse
import codeop
import sys

from agent_smith.config import load_env_file, load_sandbox_config
from agent_smith.mcp_client import MCPClient
from agent_smith.sandbox.host import Sandbox
from agent_smith.sandbox.manual import build_manual, describe_connection


def _read_entry(interactive: bool) -> str | None:
    """Read one (possibly multi-line) entry; None on EOF."""
    compiler = codeop.CommandCompiler()
    lines: list[str] = []
    while True:
        prompt = (">>> " if not lines else "... ") if interactive else ""
        try:
            line = input(prompt)
        except EOFError:
            return "\n".join(lines) if lines else None
        lines.append(line)
        source = "\n".join(lines)
        if not lines[0].strip():
            return ""
        try:
            complete = compiler(source, "<sandbox>", "exec")
        except (SyntaxError, OverflowError, ValueError):
            return source  # let the sandbox report the syntax error
        # Multi-line blocks end with an empty line, like the standard REPL.
        if complete is not None and (len(lines) == 1 or not line.strip()):
            return source


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="sandbox", description="Agent Smith restricted Python sandbox")
    parser.add_argument("config", nargs="?", help="SandboxConfig JSON file")
    group = parser.add_mutually_exclusive_group()
    group.add_argument("--mcp-stdio", metavar="CMD", help='MCP server command, e.g. "python mcp_tools_mbpp.py"')
    group.add_argument("--mcp-server", metavar="URL", help="streamable HTTP MCP server URL")
    parser.add_argument("-c", "--command", help="execute CODE once and exit")
    parser.add_argument("--manual", action="store_true", help="print the generated sandbox manual and exit")
    parser.add_argument("--env-file", help="load environment variables from this file")
    args = parser.parse_args(argv)

    load_env_file(args.env_file)
    try:
        config = load_sandbox_config(args.config)
    except Exception as exc:  # noqa: BLE001 - user-facing error
        print(f"error: invalid sandbox config: {exc}", file=sys.stderr)
        return 2

    mcp = None
    try:
        if args.mcp_stdio or args.mcp_server:
            mcp = MCPClient(stdio_command=args.mcp_stdio, url=args.mcp_server).connect()
    except Exception as exc:  # noqa: BLE001
        print(f"error: {exc}", file=sys.stderr)
        return 1

    interactive = sys.stdin.isatty() and not args.command
    try:
        with Sandbox(config, mcp) as sandbox:
            if args.manual:
                print(build_manual(config, mcp))
                return 0
            if args.command is not None:
                return _run_once(sandbox, args.command)
            if interactive:
                print(f"Agent Smith sandbox — {describe_connection(mcp)}")
                print("Type 'exit' or press Ctrl+D to quit.")
            while True:
                try:
                    entry = _read_entry(interactive)
                except KeyboardInterrupt:
                    print("\nKeyboardInterrupt")
                    continue
                if entry is None or entry.strip() in ("exit", "exit()", "quit", "quit()"):
                    break
                if not entry.strip():
                    continue
                try:
                    result = sandbox.execute(entry)
                except KeyboardInterrupt:
                    print("KeyboardInterrupt")
                    sandbox.restart()
                    continue
                except SystemExit as exc:
                    return exc.code if isinstance(exc.code, int) else 0
                text = result.render().replace("[final_answer received]", "")
                if not text.startswith("(code executed successfully"):
                    print(text, end="")
                if result.is_final:
                    print(f"[final_answer] {result.final_answer}", end="")
                if text.strip() or result.is_final:
                    print()
    finally:
        if mcp:
            mcp.close()
    if interactive:
        print()
    return 0


def _run_once(sandbox: Sandbox, code: str) -> int:
    try:
        result = sandbox.execute(code)
    except SystemExit as exc:
        return exc.code if isinstance(exc.code, int) else 0
    print(result.render())
    return 0 if result.status in ("ok", "final_answer") else 1


if __name__ == "__main__":
    sys.exit(main())
