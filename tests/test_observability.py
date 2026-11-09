"""Execution trace (interpreter path) and MCP execution reports used by the UI."""

import os

from agent_smith.mcp_client import MCPClient
from agent_smith.models import SandboxConfig
from agent_smith.sandbox import Sandbox
from tests.conftest import FIXTURES, ROOT


def test_trace_records_path_branches_and_variable_changes():
    with Sandbox(SandboxConfig(), trace_execution=True) as sb:
        r = sb.execute("def f(n):\n    s = 0\n    for i in range(n):\n        s += i\n    return s\n"
                       "x = f(3)\nif x > 10:\n    print('big')\nelse:\n    print('small')")
        entries = r.trace["entries"]
        lines = [e["l"] for e in entries if e["e"] == "line" and e["f"] == "<module>"]
        assert lines == [1, 6, 7, 10] and 8 not in lines  # the `if` branch was skipped
        assert any(e.get("v", {}).get("x") == "3" and e["l"] == 6 for e in entries)  # change attributed to line 6
        assert any(e["e"] == "return" and e["r"] == "3" for e in entries)
        # variables persist: no spurious "changes" on the next execution
        assert sb.execute("y = x + 1").trace["entries"][0]["v"] == {"y": "4"}


def test_trace_disabled_by_default_and_never_runs_user_repr():
    with Sandbox() as sb:
        assert sb.execute("print(1)").trace is None
    with Sandbox(trace_execution=True) as sb:
        r = sb.execute("class A:\n    def __repr__(self):\n        print('repr called')\n        return 'A'\na = A()")
        assert "repr called" not in r.stdout


def test_tool_results_carry_hops_and_provenance(tmp_path):
    events = []
    env = dict(os.environ, MBPP_TASK_FILE=str(FIXTURES / "mbpp_task_282.json"))
    with MCPClient(stdio_command=f"python {ROOT / 'mcp_tools_mbpp.py'}", env=env) as mcp, \
            Sandbox(SandboxConfig(), mcp, event_hook=lambda k, d: events.append((k, d)), trace_execution=True) as sb:
        r = sb.execute("code = 'def sub_list(a, b):\\n    return [x - y for x, y in zip(a, b)]'\nprint(run_tests(code))")
    assert [e["e"] for e in r.trace["entries"]].count("tool") == 1
    call = next(d for k, d in events if k == "tool_call")
    res = next(d for k, d in events if k == "tool_result")
    assert call["written"] == "run_tests('def sub_list(a, b):\\n    return [x - y for x, y in zip(a, b)]')"
    assert res["request"]["method"] == "tools/call" and res["request"]["params"]["arguments"]["code"].startswith("def")
    meta = res["server"]
    assert meta["handler"] == "mbpp_tools.run_tests" and meta["ops"][0]["op"] == "sandbox"
    assert "task.json of MBPP task 282" in meta["details"]["source"]
    assert meta["details"]["tests"] == ["assert sub_list([1,2],[3,4])==[-2,-2]", "assert sub_list([90,120],[50,70])==[40,50]"]


def test_swebench_ops_report_commands(git_repo):
    env = dict(os.environ, TESTBED_PATH=str(git_repo))
    with MCPClient(stdio_command=f"python {ROOT / 'mcp_tools_swebench.py'}", env=env) as mcp:
        out = mcp.call_tool("search_code", {"pattern": "def add"})
    assert out.meta["executor"] == "local" and out.meta["ops"][0]["command"].count("grep") == 1
    assert out.meta["ops"][0]["exit_code"] == 0 and out.rtt_ms >= out.meta["duration_ms"]
