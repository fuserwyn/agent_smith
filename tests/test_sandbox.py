"""Security and behaviour tests for the sandbox (mirrors exam_sandbox.sh categories)."""

import pytest

from agent_smith.models import SandboxConfig
from agent_smith.sandbox import Sandbox


@pytest.fixture(scope="module")
def sb():
    with Sandbox(SandboxConfig(max_execution_time_seconds=2, max_memory_mb=128)) as sandbox:
        yield sandbox


def run(sb, code):
    return sb.execute(code)


# ---------- import block ----------
@pytest.mark.parametrize("code", ["import os", "import subprocess", "from os import system", "import socket",
                                  "import importlib", "__import__('os')", "import sys"])
def test_import_block(sb, code):
    r = run(sb, code)
    assert r.status in ("security_error", "error"), r.render()


def test_allowed_imports_and_submodules(sb):
    r = run(sb, "import math, json, re, heapq\nfrom collections import Counter, deque\nimport collections.abc\n"
                "from math import *\nprint(floor(sqrt(17)), Counter('aab')['a'], json.dumps([1]))")
    assert r.status == "ok" and "4 2 [1]" in r.stdout


@pytest.mark.parametrize("code", ["import random\nrandom._os.system('id')", "import typing\nprint(typing.sys)",
                                  "import json\njson.decoder.re  # allowed module is fine\nimport collections\n"
                                  "collections._sys"])
def test_module_escape_through_attributes(sb, code):
    r = run(sb, code)
    assert r.status == "error" and "not allowed" in r.render()


# ---------- builtin block ----------
@pytest.mark.parametrize("code", ["eval('1')", "exec('x=1')", "compile('1','a','eval')", "globals()", "vars()",
                                  "breakpoint()", "input()", "help()"])
def test_dangerous_builtins_removed(sb, code):
    r = run(sb, code)
    assert r.status == "error" and "NameError" in r.render()


@pytest.mark.parametrize("code", ["().__class__.__bases__[0].__subclasses__()", "print.__self__",
                                  "(lambda: 1).__globals__", "__builtins__", "getattr(1, '__class__')",
                                  "def g():\n    yield 1\ng().gi_frame.f_globals",
                                  "try:\n    1/0\nexcept Exception as e:\n    e.__traceback__.tb_frame"])
def test_introspection_escapes_blocked(sb, code):
    r = run(sb, code)
    assert r.status in ("security_error", "error"), r.render()
    assert "not allowed" in r.render() or "NameError" in r.render()


# ---------- network block ----------
def test_network_blocked_even_if_socket_was_allowed():
    cfg = SandboxConfig(authorized_imports=["socket"], max_execution_time_seconds=5)
    with Sandbox(cfg) as s:
        r = s.execute("import socket\nsocket.create_connection(('1.1.1.1', 80), timeout=2)")
    assert r.status == "error" and "network access is disabled" in r.render()


# ---------- path restriction ----------
def test_path_restrictions(sb):
    assert "denied" in run(sb, "open('/etc/passwd').read()").render()
    assert "denied" in run(sb, "open('/tmp/agent/../../etc/hosts').read()").render()
    assert "denied" in run(sb, "open(3, 'w')").render()  # raw fds would expose the protocol channel
    ok = run(sb, "with open('/tmp/agent/t.txt', 'w') as f:\n    f.write('hi')\nprint(open('/tmp/agent/t.txt').read())")
    assert ok.status == "ok" and ok.stdout.strip() == "hi"


def test_os_module_blocked_even_if_allowed():
    cfg = SandboxConfig(authorized_imports=["os"], max_execution_time_seconds=5)
    with Sandbox(cfg) as s:
        assert "not allowed" in s.execute("import os\nos.system('echo pwned')").render()
        assert "denied" in s.execute("import os\nos.listdir('/etc')").render()
        assert "denied" in s.execute("import os\nos.remove('/etc/hosts')").render()
        env = s.execute("import os\nprint(dict(os.environ))").stdout
        assert "API_KEY" not in env


# ---------- timeout ----------
def test_timeout_with_partial_output_and_state_kept(sb):
    run(sb, "keep = 7")
    r = run(sb, "print('partial')\nwhile True:\n    pass")
    assert r.status == "timeout" and "partial" in r.stdout and "PARTIAL" in r.render()
    assert run(sb, "print(keep)").stdout.strip() == "7"


def test_timeout_cannot_be_swallowed(sb):
    r = run(sb, "try:\n    while True: pass\nexcept Exception:\n    print('caught')")
    assert r.status == "timeout" and "caught" not in r.stdout


# ---------- memory ----------
def test_memory_limit(sb):
    r = run(sb, "x = bytearray(400 * 1024 * 1024)")
    assert r.status == "memory" and "memory limit" in r.render()
    assert run(sb, "print('alive')").stdout.strip() == "alive"


# ---------- misc behaviour ----------
def test_persistence_last_expression_and_errors(sb):
    run(sb, "def sq(n):\n    return n * n")
    assert run(sb, "sq(9)").value == "81"
    r = run(sb, "def f():\n    return 1/0\nf()")
    assert "ZeroDivisionError" in r.render() and "Line" in r.render()
    assert run(sb, "def (:").status == "syntax_error"


def test_classes_and_super_work(sb):
    r = run(sb, "class A:\n    def __init__(self):\n        self._v = 1\nclass B(A):\n    def __init__(self):\n"
                "        super().__init__()\n        self._v += 1\nprint(B()._v)")
    assert r.stdout.strip() == "2"


def test_final_answer_string_and_function(sb):
    r = run(sb, "final_answer('done')")
    assert r.is_final and r.final_answer == "done"
    r = run(sb, "import math\ndef area(r):\n    return math.pi * r * r\nfinal_answer(area)")
    assert r.is_final and "def area" in r.final_answer and "import math" in r.final_answer


def test_final_answer_survives_broad_except(sb):
    r = run(sb, "try:\n    final_answer('x')\nexcept BaseException:\n    pass")
    assert r.is_final and r.final_answer == "x"


def test_output_truncation_is_reported():
    with Sandbox(SandboxConfig(max_output_chars=100)) as s:
        r = s.execute("print('a' * 1000)")
    assert r.truncated and "truncated" in r.render()


def test_system_exit_and_keyboard_interrupt_propagate():
    with Sandbox() as s:
        with pytest.raises(SystemExit):
            s.execute("raise SystemExit(3)")
        with pytest.raises(KeyboardInterrupt):
            s.execute("raise KeyboardInterrupt")
        assert s.execute("print(1)").stdout.strip() == "1"
