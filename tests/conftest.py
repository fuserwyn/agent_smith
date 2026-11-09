import os
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
FIXTURES = ROOT / "tests" / "fixtures"


@pytest.fixture
def git_repo(tmp_path):
    """A small git repository used as a local 'testbed' for the SWE-bench tools."""
    repo = tmp_path / "testbed"
    (repo / "pkg").mkdir(parents=True)
    (repo / "pkg" / "__init__.py").write_text("")
    (repo / "pkg" / "mathutil.py").write_text(
        "def add(a, b):\n    return a - b\n\n\nclass Calculator:\n    def total(self, xs):\n"
        "        result = 0\n        for x in xs:\n            result = add(result, x)\n        return result\n")
    (repo / "tests").mkdir()
    (repo / "tests" / "test_mathutil.py").write_text(
        "from pkg.mathutil import add\n\n\ndef test_add():\n    assert add(2, 3) == 5\n")
    env = dict(os.environ, GIT_AUTHOR_NAME="t", GIT_AUTHOR_EMAIL="t@t", GIT_COMMITTER_NAME="t",
               GIT_COMMITTER_EMAIL="t@t")
    for cmd in (["git", "init", "-q"], ["git", "add", "."], ["git", "commit", "-qm", "init"]):
        subprocess.run(cmd, cwd=repo, check=True, env=env)
    return repo
