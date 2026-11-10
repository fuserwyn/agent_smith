"""End-to-end agent runs with a fake OpenAI-compatible server (no real LLM, no Docker)."""

import json
import os
import subprocess
import sys

from agent_smith.agent import AgentSettings, CodeAgent
from agent_smith.agent.prompts import swebench_system_prompt
from agent_smith.events import EventSink
from agent_smith.llm import Endpoint, KeyPool, LLMClient, OpenAICompatProvider
from agent_smith.mcp_client import MCPClient
from agent_smith.models import MBPP_LIMITS, SWEBENCH_LIMITS, Limits, SandboxConfig
from agent_smith.sandbox import Sandbox
from tests.conftest import FIXTURES, ROOT
from tests.fake_llm_server import FakeLLMServer

GOOD = ('Thought: subtract.\n```python\ncode = r"""\ndef sub_list(a, b):\n    return [x - y for x, y in zip(a, b)]\n'
        '"""\nresult = run_tests(code)\nprint(result)\nif result.startswith("ALL TESTS PASSED"):\n'
        '    final_answer(code)\n```<end_code>\nObservation: (hallucinated, must be cut by the stop sequence)')
BAD = GOOD.replace("x - y", "x + y")


def run_mbpp_cli(tmp_path, script, *extra):
    out = tmp_path / "solution.json"
    with FakeLLMServer(script) as srv:
        env = dict(os.environ, LLM_API_KEY="test-key")
        proc = subprocess.run([sys.executable, "-m", "agent_mbpp", "--task-file", str(FIXTURES / "mbpp_task_282.json"),
                               "--output", str(out), "--model-name", "fake", "--provider-url", srv.url, *extra],
                              capture_output=True, text=True, env=env, cwd=ROOT, timeout=120)
        requests = srv.requests
    return json.loads(out.read_text()), proc, requests


def test_mbpp_cli_fix_after_failure(tmp_path):
    sol, proc, requests = run_mbpp_cli(tmp_path, [BAD, GOOD], "--trace-file", str(tmp_path / "t.jsonl"))
    assert proc.returncode == 0, proc.stderr
    assert sol["success"] and "x - y" in sol["solution"] and sol["iterations"] == 2
    assert sol["total_input_tokens"] == sum(s["input_tokens"] for s in sol["steps"]) <= MBPP_LIMITS.max_input_tokens
    assert "got:" in sol["steps"][0]["sandbox_output"]
    assert all("hallucinated" not in s["llm_output"] for s in sol["steps"])
    assert sol["system_prompt"] and requests[1]["messages"][-1]["content"].startswith("Observation:")
    events = [json.loads(l)["type"] for l in (tmp_path / "t.jsonl").read_text().splitlines()]
    assert events[0] == "mcp_connected" and "tool_call" in events and events[-1] == "run_end"


def test_mbpp_no_code_feedback_and_fallback_candidate(tmp_path):
    no_code = "I would subtract the lists."
    sol, proc, requests = run_mbpp_cli(tmp_path, [no_code, BAD], "--max-iterations", "2")
    assert not sol["success"] and sol["iterations"] == 2
    assert "No valid code block" in requests[1]["messages"][-1]["content"]
    assert "x + y" in sol["solution"] and "best available candidate" in sol["error"]


def test_mbpp_llm_down_still_writes_solution(tmp_path):
    sol, proc, _ = run_mbpp_cli(tmp_path, [{"status": 400}])
    assert proc.returncode == 0 and not sol["success"] and "LLM failure" in sol["error"]


def test_budget_stops_before_exceeding_input_tokens(git_repo):
    """The loop must refuse a call that could exceed the cumulative input limit."""
    limits = Limits(max_iterations=30, max_input_tokens=1500, max_output_tokens=10_000, timeout_seconds=300)
    script = ['Thought: look\n```python\nprint(read_file("pkg/mathutil.py"))\n```'] * 10
    env = dict(os.environ, TESTBED_PATH=str(git_repo))
    with FakeLLMServer(script) as srv, \
            MCPClient(stdio_command=f"python {ROOT / 'mcp_tools_swebench.py'}", env=env) as mcp, \
            Sandbox(SandboxConfig(), mcp) as sb:
        llm = LLMClient([Endpoint(OpenAICompatProvider(srv.url, "fake"), KeyPool(["k"]))], sleep=lambda s: None)
        agent = CodeAgent(llm, sb, "system", limits, "swebench", "t", settings=AgentSettings())
        out = agent.run("task")
    assert out.total_input_tokens <= limits.max_input_tokens and "input-token budget" in out.error


def test_swebench_loop_on_local_repo(git_repo, tmp_path):
    script = [
        'Thought: find add.\n```python\nprint(search_function_or_class_definition_in_code("add"))\n'
        'print(read_file("pkg/mathutil.py", 1, 3))\n```<end_code>',
        'Thought: it subtracts. Fix and test.\n```python\nprint(edit_file("pkg/mathutil.py", "    return a - b", '
        '"    return a + b"))\nprint(run_tests())\n```<end_code>',
        'Thought: tests pass, submit.\n```python\nfinal_answer(get_patch())\n```<end_code>',
    ]
    eval_script = tmp_path / "eval.sh"
    eval_script.write_text(f"cd {git_repo} && {sys.executable} -m pytest -q tests 2>&1\n")
    env = dict(os.environ, TESTBED_PATH=str(git_repo), SWEBENCH_EVAL_SCRIPT_FILE=str(eval_script))
    with FakeLLMServer(script) as srv, \
            MCPClient(stdio_command=f"python {ROOT / 'mcp_tools_swebench.py'}", env=env) as mcp, \
            Sandbox(SandboxConfig(), mcp) as sb:
        llm = LLMClient([Endpoint(OpenAICompatProvider(srv.url, "fake"), KeyPool(["k"]))], sleep=lambda s: None)
        agent = CodeAgent(llm, sb, swebench_system_prompt(SandboxConfig(), mcp, str(git_repo)), SWEBENCH_LIMITS,
                          "swebench", "local-1", settings=AgentSettings(show_budget=True),
                          events=EventSink(str(tmp_path / "trace.jsonl")))
        out = agent.run("Fix add()")
    assert out.success and out.iterations == 3
    assert out.solution.startswith("diff --git") and "+    return a + b" in out.solution
    assert "1 passed" in out.steps[1].sandbox_output and "[budget]" in out.steps[0].sandbox_output


def test_exact_repeat_is_not_executed_and_gets_a_navigation_hint(git_repo):
    step = 'Thought: find it\n```python\nprint(search_code("def add(a, b):"))\n```<end_code>'
    fix = ('Thought: fix\n```python\nprint(edit_file("pkg/mathutil.py", "return a - b", "return a + b"))\n'
           'final_answer(get_patch())\n```<end_code>')
    env = dict(os.environ, TESTBED_PATH=str(git_repo))
    with FakeLLMServer([step, step, fix]) as srv, \
            MCPClient(stdio_command=f"python {ROOT / 'mcp_tools_swebench.py'}", env=env) as mcp, \
            Sandbox(SandboxConfig(), mcp) as sb:
        llm = LLMClient([Endpoint(OpenAICompatProvider(srv.url, "fake"), KeyPool(["k"]))], sleep=lambda s: None)
        out = CodeAgent(llm, sb, "system", SWEBENCH_LIMITS, "swebench", "t").run("task")
    assert out.success and "[LOOP]" in out.steps[1].sandbox_output
    assert "read_file(" in out.steps[1].sandbox_output and "mathutil.py" in out.steps[1].sandbox_output
    assert [c.name for c in sb.tool_log].count("search_code") == 1  # the repeat did not reach the tool
