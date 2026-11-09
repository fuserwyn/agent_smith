"""MBPP tools: run a candidate solution against assert-style tests.

The candidate is LLM-generated, therefore untrusted: it is executed inside the
same restricted sandbox (without MCP), never with a bare ``subprocess``.
"""

from __future__ import annotations

import ast
import json
import os
import time
from typing import List, Optional

from agent_smith.models import MBPPTaskInput, SandboxConfig
from agent_smith.sandbox.host import Sandbox

TEST_TIMEOUT_S = 10
OPS: List[dict] = []  # execution report of the last call (read by the MCP server for _meta)
DETAILS: dict = {}    # where the tests came from + the generated harness (for _meta)


def load_task_from_env() -> Optional[MBPPTaskInput]:
    path = os.environ.get("MBPP_TASK_FILE")
    if not path or not os.path.isfile(path):
        return None
    with open(path, encoding="utf-8") as fh:
        return MBPPTaskInput.model_validate(json.load(fh))


def _test_block(index: int, test: str) -> str:
    """Turn ``assert f(x) == y`` into code that reports PASS/FAIL with the actual value."""
    label = json.dumps(test.strip())
    try:
        node = ast.parse(test.strip()).body[0]
    except (SyntaxError, IndexError):
        node = None
    if (isinstance(node, ast.Assert) and isinstance(node.test, ast.Compare) and len(node.test.ops) == 1
            and isinstance(node.test.ops[0], ast.Eq)):
        left = ast.unparse(node.test.left)
        right = ast.unparse(node.test.comparators[0])
        body = (f"    _got = {left}\n    _exp = {right}\n"
                f"    if _got == _exp:\n        print('PASS {index}: ' + {label})\n"
                f"    else:\n        print('FAIL {index}: ' + {label} + '\\n    got:      ' + repr(_got)[:300]"
                f" + '\\n    expected: ' + repr(_exp)[:300])\n")
    else:
        body = f"    {test.strip()}\n    print('PASS {index}: ' + {label})\n"
    return (f"try:\n{body}except AssertionError:\n    print('FAIL {index}: ' + {label})\n"
            f"except Exception as _e:\n    print('ERROR {index}: ' + {label} + '\\n    ' + "
            f"type(_e).__name__ + ': ' + str(_e)[:300])\n")


def run_tests(code: str, tests: Optional[List[str]] = None, test_imports: Optional[List[str]] = None,
              config: Optional[SandboxConfig] = None) -> str:
    """Execute ``code`` then each test; return a readable report."""
    if not isinstance(code, str) or not code.strip():
        return "Error: 'code' must be a non-empty string containing the full solution (imports + function)."
    DETAILS.clear()
    if tests is None:
        task = load_task_from_env()
        if task is None:
            return "Error: no tests given and no MBPP task configured (MBPP_TASK_FILE)."
        tests, test_imports = task.test_list, task.test_imports
        DETAILS["source"] = (f"task.json of MBPP task {task.task_id} (dumped by the moulinette, read via MBPP_TASK_FILE): "
                             f"public test_list from the MBPP dataset. The moulinette hides the first test; "
                             f"it is only used at validation.")
    else:
        DETAILS["source"] = "tests passed explicitly by the caller (tests=...)"
    DETAILS["tests"] = list(tests)
    DETAILS["test_imports"] = list(test_imports or [])
    try:
        ast.parse(code)
    except SyntaxError as exc:
        return f"FAILED: the solution has a SyntaxError: {exc.msg} (line {exc.lineno}): {(exc.text or '').strip()}"
    harness = code.rstrip() + "\n\n" + "\n".join(test_imports or []) + "\n"
    harness += "\n".join(_test_block(i + 1, t) for i, t in enumerate(tests))
    cfg = (config or SandboxConfig()).model_copy(update={"max_execution_time_seconds": TEST_TIMEOUT_S,
                                                         "allowed_directories": ["/tmp/agent"]})
    DETAILS["generated"] = ("harness = candidate code + test imports + one try/except block per assert "
                            "(prints PASS/FAIL with got/expected); executed in a fresh sandbox without MCP")
    DETAILS["harness"] = harness[:6000]
    t0 = time.monotonic()
    with Sandbox(cfg) as sandbox:
        result = sandbox.execute(harness)
    OPS.append({"op": "sandbox", "command": f"Sandbox.execute(solution + {len(tests)} test assertions, "
                f"{len(harness.splitlines())} lines)", "exit_code": 0 if result.status == "ok" else 1,
                "duration_ms": round((time.monotonic() - t0) * 1000, 1),
                "stdout_bytes": len(result.stdout.encode()), "stderr_bytes": len((result.error or "").encode()),
                "stdin_bytes": len(harness.encode()), "stdout_head": result.stdout[:300],
                "stderr_head": (result.error or "")[:200], "timed_out": result.status == "timeout"})
    lines = [l for l in result.stdout.splitlines()]
    passed = sum(1 for l in lines if l.startswith("PASS "))
    report = "\n".join(lines)
    if result.error:
        report += ("\n" if report else "") + result.error
    if result.status == "timeout":
        report += "\n[tests timed out: the solution is too slow or loops forever]"
    verdict = (f"ALL TESTS PASSED ({passed}/{len(tests)})" if passed == len(tests) and len(tests) > 0
               and result.status == "ok" else f"TESTS FAILED ({passed}/{len(tests)} passed)")
    return f"{verdict}\n{report}".rstrip()
