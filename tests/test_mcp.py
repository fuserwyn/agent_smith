"""MCP integration: discovery, tool calls from inside the sandbox, stdio and streamable HTTP."""

import os
import socket
import subprocess
import sys
import time

import pytest

from agent_smith.mcp_client import MCPClient
from agent_smith.models import SandboxConfig
from agent_smith.sandbox import Sandbox, build_manual
from tests.conftest import FIXTURES, ROOT

MANDATORY = {"read_file", "edit_file", "list_files", "search_code", "search_function_or_class_definition_in_code",
             "find_references", "run_tests", "get_patch", "run_command"}


def swe_client(repo):
    env = dict(os.environ, TESTBED_PATH=str(repo))
    env.pop("SWEBENCH_CONTAINER", None)
    return MCPClient(stdio_command=f"python {ROOT / 'mcp_tools_swebench.py'}", env=env)


def test_swebench_server_exposes_mandatory_tools_and_manual(git_repo):
    with swe_client(git_repo) as mcp:
        assert MANDATORY <= set(mcp.tool_names())
        manual = build_manual(SandboxConfig(), mcp)
        assert "read_file(filepath: str" in manual and "final_answer" in manual


def test_tools_callable_from_sandbox_positional_and_keyword(git_repo):
    with swe_client(git_repo) as mcp, Sandbox(SandboxConfig(), mcp) as sb:
        r = sb.execute("print(read_file('pkg/mathutil.py', 1, 2))\nprint(search_code(pattern='class Calc'))")
        assert "1: def add(a, b):" in r.stdout and "mathutil.py:5 class Calculator:" in r.stdout
        assert [c.name for c in r.tool_calls] == ["read_file", "search_code"]
        r = sb.execute("edit_file('pkg/mathutil.py', 'return a - b', 'return a + b')\nfinal_answer(get_patch())")
        assert r.is_final and "+    return a + b" in r.final_answer
        bad = sb.execute("read_file()")
        assert "missing required argument" in bad.render()


def test_tool_time_does_not_count_toward_sandbox_timeout(git_repo):
    cfg = SandboxConfig(max_execution_time_seconds=1)
    with swe_client(git_repo) as mcp, Sandbox(cfg, mcp) as sb:
        r = sb.execute("print(run_command('sleep 2; echo slept'))")
    assert r.status == "ok" and "slept" in r.stdout


def _free_port():
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def test_streamable_http_transport_with_mbpp_server():
    port = _free_port()
    env = dict(os.environ, MBPP_TASK_FILE=str(FIXTURES / "mbpp_task_282.json"))
    proc = subprocess.Popen([sys.executable, str(ROOT / "mcp_tools_mbpp.py"), "--http", "--port", str(port)],
                            env=env, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    try:
        for _ in range(100):
            try:
                socket.create_connection(("127.0.0.1", port), timeout=0.2).close()
                break
            except OSError:
                time.sleep(0.1)
        with MCPClient(url=f"http://127.0.0.1:{port}/mcp") as mcp, Sandbox(SandboxConfig(), mcp) as sb:
            assert mcp.tool_names() == ["run_tests"] and mcp.has_resources and mcp.has_prompts
            r = sb.execute("code = 'def sub_list(a, b):\\n    return [x - y for x, y in zip(a, b)]'\n"
                           "print(run_tests(code))\nprint(read_resource('mbpp://task')[:20])")
            assert "ALL TESTS PASSED (2/2)" in r.stdout and "task_id" in r.stdout
    finally:
        proc.terminate()
        proc.wait(timeout=10)


def test_unknown_server_tools_are_discovered_dynamically(tmp_path):
    server = tmp_path / "other_server.py"
    server.write_text(
        "from mcp.server.fastmcp import FastMCP\nmcp = FastMCP('other', log_level='WARNING')\n"
        "@mcp.tool()\ndef shout(text: str, times: int = 2) -> str:\n    '''Upper-case text.'''\n"
        "    return (text.upper() + ' ') * times\nmcp.run()\n")
    with MCPClient(stdio_command=f"python {server}") as mcp, Sandbox(SandboxConfig(), mcp) as sb:
        assert "shout(text: str, times: int = 2)" in build_manual(SandboxConfig(), mcp)
        assert sb.execute("print(shout('hi', 3))").stdout.strip() == "HI HI HI"
        assert "read_file" not in sb.execute("print(sorted(n for n in dir() if not n.startswith('_')))").stdout
