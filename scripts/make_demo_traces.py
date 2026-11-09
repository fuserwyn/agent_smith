"""Produce the demo traces embedded in ui/index.html.

The traces come from REAL executions of the framework (sandbox worker, MCP
servers, tools, budget accounting); only the LLM answers are scripted, served
by the fake OpenAI-compatible server used in the tests. They are labelled as
such in the UI and are never used as benchmark data.

    uv run python scripts/make_demo_traces.py
"""

from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "src"))

from agent_smith.agent import AgentSettings, CodeAgent  # noqa: E402
from agent_smith.agent.prompts import swebench_system_prompt  # noqa: E402
from agent_smith.events import EventSink  # noqa: E402
from agent_smith.llm import Endpoint, KeyPool, LLMClient, OpenAICompatProvider  # noqa: E402
from agent_smith.mcp_client import MCPClient  # noqa: E402
from agent_smith.models import SWEBENCH_LIMITS, SandboxConfig  # noqa: E402
from agent_smith.sandbox import Sandbox  # noqa: E402
from tests.fake_llm_server import FakeLLMServer  # noqa: E402

UI = ROOT / "ui" / "index.html"

MBPP_SCRIPT = [
    'Thought: Pair the two lists element-wise and combine each pair.\n```python\ncode = r"""\n'
    'def sub_list(nums1, nums2):\n    return list(map(lambda x, y: x + y, nums1, nums2))\n"""\n'
    'result = run_tests(code)\nprint(result)\nif result.startswith("ALL TESTS PASSED"):\n    final_answer(code)\n'
    '```<end_code>',
    'Thought: The tests show sums instead of differences: the lambda must subtract.\n```python\ncode = r"""\n'
    'def sub_list(nums1, nums2):\n    return list(map(lambda x, y: x - y, nums1, nums2))\n"""\n'
    'result = run_tests(code)\nprint(result)\nif result.startswith("ALL TESTS PASSED"):\n    final_answer(code)\n'
    '```<end_code>',
]

SWE_SCRIPT = [
    'Thought: The issue says Calculator.total returns wrong sums. Locate total() and add().\n```python\n'
    'print(search_function_or_class_definition_in_code("Calculator"))\n'
    'print(search_function_or_class_definition_in_code("add"))\n```<end_code>',
    'Thought: Read the module around both definitions.\n```python\nprint(read_file("pkg/mathutil.py", 1, 12))\n'
    '```<end_code>',
    'Thought: Reproduce before fixing.\n```python\nprint(run_command("python -c \'from pkg.mathutil import '
    'Calculator; print(Calculator().total([1, 2, 3]))\'"))\n```<end_code>',
    'Thought: total() is fine; add() subtracts. Fix the root cause and run the tests.\n```python\n'
    'print(edit_file("pkg/mathutil.py", "    return a - b", "    return a + b"))\nprint(run_tests())\n```<end_code>',
    'Thought: Tests pass. Submit the patch.\n```python\nfinal_answer(get_patch())\n```<end_code>',
]

ISSUE = ("Calculator.total() returns wrong results\n\n`Calculator().total([1, 2, 3])` returns -6 instead of 6. "
         "Expected the sum of the values.")


def run_mbpp(tmp: Path) -> list:
    trace = tmp / "mbpp.jsonl"
    with FakeLLMServer(MBPP_SCRIPT, model="scripted-demo") as srv:
        env = dict(os.environ, LLM_API_KEY="demo")
        subprocess.run([sys.executable, "-m", "agent_mbpp", "--task-file",
                        str(ROOT / "tests" / "fixtures" / "mbpp_task_282.json"), "--output", str(tmp / "mbpp.json"),
                        "--model-name", "scripted-demo", "--provider-url", srv.url, "--trace-file", str(trace)],
                       check=True, cwd=ROOT, env=env, capture_output=True)
    return [json.loads(l) for l in trace.read_text().splitlines()]


def make_repo(path: Path) -> None:
    if path.exists():
        shutil.rmtree(path)
    (path / "pkg").mkdir(parents=True)
    (path / "tests").mkdir()
    (path / "pkg" / "__init__.py").write_text("")
    (path / "pkg" / "mathutil.py").write_text(
        '"""Small arithmetic helpers."""\n\n\ndef add(a, b):\n    return a - b\n\n\nclass Calculator:\n'
        "    def total(self, xs):\n        result = 0\n        for x in xs:\n            result = add(result, x)\n"
        "        return result\n")
    (path / "tests" / "test_mathutil.py").write_text(
        "from pkg.mathutil import Calculator, add\n\n\ndef test_add():\n    assert add(2, 3) == 5\n\n\n"
        "def test_total():\n    assert Calculator().total([1, 2, 3]) == 6\n")
    env = dict(os.environ, GIT_AUTHOR_NAME="demo", GIT_AUTHOR_EMAIL="demo@example.com",
               GIT_COMMITTER_NAME="demo", GIT_COMMITTER_EMAIL="demo@example.com")
    for cmd in (["git", "init", "-q"], ["git", "add", "."], ["git", "commit", "-qm", "init"]):
        subprocess.run(cmd, cwd=path, check=True, env=env)


def run_swe(tmp: Path) -> list:
    repo = Path("/tmp/agent_smith_demo/testbed")
    make_repo(repo)
    eval_script = tmp / "eval.sh"
    eval_script.write_text(f"cd {repo}\necho '>>>>> Start Test Output'\n{sys.executable} -m pytest -rA -q tests 2>&1\n"
                           "echo '>>>>> End Test Output'\n")
    trace = tmp / "swe.jsonl"
    events = EventSink(str(trace))
    env = dict(os.environ, TESTBED_PATH=str(repo), SWEBENCH_EVAL_SCRIPT_FILE=str(eval_script))
    with FakeLLMServer(SWE_SCRIPT, model="scripted-demo") as srv:
        llm = LLMClient([Endpoint(OpenAICompatProvider(srv.url, "scripted-demo"), KeyPool(["demo"]))],
                        on_event=events.emit)
        with MCPClient(stdio_command=f"python {ROOT / 'mcp_tools_swebench.py'}", env=env) as mcp:
            events.emit("mcp_connected", {"server": mcp.server_name, "tools": mcp.tool_names(),
                                          "resources": mcp.has_resources, "prompts": mcp.has_prompts})
            with Sandbox(SandboxConfig(), mcp, event_hook=events.emit, trace_execution=True) as sb:
                agent = CodeAgent(llm, sb, swebench_system_prompt(SandboxConfig(), mcp, str(repo)), SWEBENCH_LIMITS,
                                  "swebench", "demo__calculator-1", settings=AgentSettings(show_budget=True),
                                  events=events)
                agent.run(f"Repository: demo/calculator (checked out at {repo})\n<issue>\n{ISSUE}\n</issue>")
    events.close()
    return [json.loads(l) for l in trace.read_text().splitlines()]


def main() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        traces = {"mbpp": run_mbpp(Path(tmp)), "swebench": run_swe(Path(tmp))}
    data = json.dumps(traces, separators=(",", ":"))
    # Keep the published demo free of local machine details.
    data = re.sub(r"http://127\.0\.0\.1:\d+/v1", "scripted://fake-llm", data)
    data = data.replace(json.dumps(sys.executable)[1:-1], "python").replace(json.dumps(str(ROOT))[1:-1], "<repo>")
    data = re.sub(r"/(?:private/)?var/folders/[^\s\"\\]+", "<tmp>", data)
    data = re.sub(r"/(?:private/)?tmp/tmp[\w-]+", "<tmp>", data)
    data = data.replace("</", "<\\/")
    html = UI.read_text(encoding="utf-8")
    html = re.sub(r"/\*DEMO_START\*/.*?/\*DEMO_END\*/", lambda m: f"/*DEMO_START*/{data}/*DEMO_END*/", html, flags=re.S)
    UI.write_text(html, encoding="utf-8")
    print(f"embedded {sum(len(v) for v in traces.values())} events into {UI}")


if __name__ == "__main__":
    main()
