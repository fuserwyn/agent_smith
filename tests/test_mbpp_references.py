"""Regression: reference MBPP solutions must pass through our sandboxed run_tests.

Guards against a sandbox that is so strict it rejects legitimate solutions.
Uses the moulinette dataset when present (set MBPP_ALL=1 to check every task).
"""

import json
import os
import random

import pytest

from agent_smith.tools.mbpp_tools import run_tests
from tests.conftest import ROOT

DATA = ROOT / "moulinette" / "moulinette" / "mbpp" / "data" / "sanitized_tasks.json"


def _tasks():
    if not DATA.exists():
        return []
    tasks = [t for t in json.loads(DATA.read_text()) if "import sys" not in t["code"]]
    return tasks if os.environ.get("MBPP_ALL") else random.Random(0).sample(tasks, 25)


@pytest.mark.skipif(not DATA.exists(), reason="moulinette dataset not available")
@pytest.mark.parametrize("task", _tasks(), ids=lambda t: str(t["task_id"]))
def test_reference_solution_passes(task):
    report = run_tests(task["code"], task["test_list"], task.get("test_imports"))
    assert report.startswith("ALL TESTS PASSED"), report
