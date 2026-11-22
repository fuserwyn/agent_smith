"""End-to-end agent runs with a fake OpenAI-compatible server (no real LLM, no Docker)."""

import shlex
import json
import os
import subprocess
import sys

from agent_smith.agent import AgentSettings, CodeAgent
from agent_smith.agent.prompts import swe_system_prompt
from agent_smith.events import EventSink
from agent_smith.llm import Endpoint, KeyPool, LLMClient, OpenAICompatProvider
from agent_smith.mcp_client import MCPClient
from agent_smith.models import MBPP_LIMITS, SWE_BENCH, SWEBENCH_LIMITS, Limits, SandboxConfig
from agent_smith.sandbox import Sandbox
from tests.conftest import FIXTURES, ROOT
from tests.fake_llm_server import FakeLLMServer

GOOD = ('Thought: subtract.\n```python\nimport json\ncode = r"""\ndef sub_list(a, b):\n    return [x - y for x, y in zip(a, b)]\n'
        '"""\nresult = json.loads(run_tests(code))\nprint(result["output"])\nif result["success"]:\n'
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
    # nothing ran, but the feedback sent to the LLM is logged for the evaluators
    assert sol["steps"][0]["sandbox_input"] == "" and "No valid code block" in sol["steps"][0]["sandbox_output"]
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
            MCPClient(stdio_command=f"python {shlex.quote(str(ROOT / 'mcp_tools_swebench.py'))}", env=env) as mcp, \
            Sandbox(SandboxConfig(), mcp) as sb:
        llm = LLMClient([Endpoint(OpenAICompatProvider(srv.url, "fake"), KeyPool(["k"]))], sleep=lambda s: None)
        agent = CodeAgent(llm, sb, "system", limits, SWE_BENCH, "t", settings=AgentSettings())
        out = agent.run("task")
    assert out.total_input_tokens <= limits.max_input_tokens and "input-token budget" in out.error


def test_swe_loop_on_local_repo(git_repo, tmp_path):
    script = [
        'Thought: find add.\n```python\nprint(search_function_or_class_definition_in_code("add"))\n'
        'print(read_file("pkg/mathutil.py", 1, 3))\n```<end_code>',
        'Thought: it subtracts. Fix and test.\n```python\nprint(edit_file("pkg/mathutil.py", "    return a - b", '
        '"    return a + b"))\nprint(run_tests())\n```<end_code>',
        'Thought: tests pass, submit.\n```python\nfinal_answer(get_patch())\n```<end_code>',
    ]
    eval_script = tmp_path / "eval.sh"
    eval_script.write_text(f"cd {shlex.quote(str(git_repo))} && {shlex.quote(sys.executable)} -m pytest -q tests 2>&1\n")
    env = dict(os.environ, TESTBED_PATH=str(git_repo), SWEBENCH_EVAL_SCRIPT_FILE=str(eval_script))
    with FakeLLMServer(script) as srv, \
            MCPClient(stdio_command=f"python {shlex.quote(str(ROOT / 'mcp_tools_swebench.py'))}", env=env) as mcp, \
            Sandbox(SandboxConfig(), mcp) as sb:
        llm = LLMClient([Endpoint(OpenAICompatProvider(srv.url, "fake"), KeyPool(["k"]))], sleep=lambda s: None)
        agent = CodeAgent(llm, sb, swe_system_prompt(SandboxConfig(), mcp, str(git_repo)), SWEBENCH_LIMITS,
                          SWE_BENCH, "local-1", settings=AgentSettings(show_budget=True),
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
            MCPClient(stdio_command=f"python {shlex.quote(str(ROOT / 'mcp_tools_swebench.py'))}", env=env) as mcp, \
            Sandbox(SandboxConfig(), mcp) as sb:
        llm = LLMClient([Endpoint(OpenAICompatProvider(srv.url, "fake"), KeyPool(["k"]))], sleep=lambda s: None)
        out = CodeAgent(llm, sb, "system", SWEBENCH_LIMITS, SWE_BENCH, "t").run("task")
    assert out.success and "[LOOP]" in out.steps[1].sandbox_output
    assert "read_file(" in out.steps[1].sandbox_output and "mathutil.py" in out.steps[1].sandbox_output
    assert [c.name for c in sb.tool_log].count("search_code") == 1  # the repeat did not reach the tool


def test_rejected_final_answer_and_empty_response(git_repo):
    """A description instead of a patch is refused; an empty LLM reply never becomes an empty message."""
    describe = 'Thought: done\n```python\nfinal_answer("I fixed the typo in add()")\n```<end_code>'
    fix = ('Thought: fix\n```python\nprint(edit_file("pkg/mathutil.py", "return a - b", "return a + b"))\n'
           'final_answer(get_patch())\n```<end_code>')
    env = dict(os.environ, TESTBED_PATH=str(git_repo))
    with FakeLLMServer(["", describe, fix]) as srv, \
            MCPClient(stdio_command=f"python {shlex.quote(str(ROOT / 'mcp_tools_swebench.py'))}", env=env) as mcp, \
            Sandbox(SandboxConfig(), mcp) as sb:
        llm = LLMClient([Endpoint(OpenAICompatProvider(srv.url, "fake"), KeyPool(["k"]))], sleep=lambda s: None)
        out = CodeAgent(llm, sb, "system", SWEBENCH_LIMITS, SWE_BENCH, "t",
                        answer_validator=lambda a: None if "diff --git" in a else "must be the patch").run("task")
        sent = srv.requests
    assert out.success and out.solution.startswith("diff --git") and out.iterations == 3
    assert "[REJECTED final_answer]" in out.steps[1].sandbox_output
    assert all(m["content"] for r in sent for m in r["messages"])  # no empty message was ever sent


def test_long_runs_send_only_the_last_steps(tmp_path):
    """Studio turns have no step limit: the request keeps the last N steps, older ones become a note."""
    steps = [f"Thought: step {i}.\n```python\nprint({i})\n```<end_code>" for i in range(6)]
    steps.append("Thought: done.\n```python\nfinal_answer('ok')\n```<end_code>")
    unlimited = Limits(max_iterations=1_000_000, max_input_tokens=10**12, max_output_tokens=10**12, timeout_seconds=3600)
    with FakeLLMServer(steps) as srv, Sandbox(SandboxConfig(), None) as sb:
        llm = LLMClient([Endpoint(OpenAICompatProvider(srv.url, "fake"), KeyPool(["k"]))], sleep=lambda s: None)
        out = CodeAgent(llm, sb, "system", unlimited, "studio", "t", settings=AgentSettings(max_history_steps=2)).run("task")
        last = srv.requests[-1]["messages"]
    assert out.success and out.iterations == 7
    assert len(last) == 2 + 2 * 2                                      # system, task, then only 2 (assistant, observation) pairs
    assert "4 earlier step(s) of this run are not shown" in last[1]["content"]
    assert "print(5)" in last[-2]["content"] and "print(0)" not in json.dumps(last)
