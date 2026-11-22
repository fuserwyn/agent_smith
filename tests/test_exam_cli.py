"""What the official exam scripts rely on: the CLI without --model-name/--provider-url, run_tests(code, test_list)
returning JSON, and no traceback on a bad task file."""

import json
import os
import subprocess
import sys
from argparse import Namespace

import pytest

from agent_smith.runner import InputError, pick_model, read_task
from agent_smith.tools import mbpp_tools
from tests.conftest import ROOT

KEYS = ("GROQ_API_KEY", "NVIDIA_API_KEY", "OPENROUTER_API_KEY", "GEMINI_API_KEY", "COHERE_API_KEY", "LLM_API_KEY")


@pytest.fixture
def no_keys(monkeypatch):
    for k in os.environ:
        if any(k.startswith(p) for p in KEYS):
            monkeypatch.delenv(k)
    return monkeypatch


def args(model=None, url=None):
    return Namespace(model_name=model, provider_url=url)


def test_default_model_follows_the_keys_present(no_keys):
    no_keys.setenv("GROQ_API_KEY", "x")
    assert pick_model(args(), "mbpp") == ("openai/gpt-oss-120b", "https://api.groq.com/openai/v1")
    # Groq's 8k tokens/min cannot hold a SWE-bench prompt: SWE-bench needs another key
    with pytest.raises(InputError, match="NVIDIA_API_KEY"):
        pick_model(args(), "swebench")
    no_keys.setenv("OPENROUTER_API_KEY", "y")
    assert pick_model(args(), "swebench") == ("nvidia/nemotron-3.5-super-vl:free", "https://openrouter.ai/api/v1")
    no_keys.setenv("NVIDIA_API_KEY", "z")
    assert pick_model(args(), "swebench")[0] == "nvidia/nemotron-3.5-lightning-30b-a3b"


def test_explicit_and_partial_model_arguments(no_keys):
    assert pick_model(args("m", "http://localhost:11434/v1"), "mbpp") == ("m", "http://localhost:11434/v1")
    assert pick_model(args(url="https://integrate.api.nvidia.com/v1"), "swebench")[0] == \
        "nvidia/nemotron-3.5-lightning-30b-a3b"
    assert pick_model(args("gemini-3.5-flash-lite"), "mbpp")[1].startswith("https://generativelanguage")
    with pytest.raises(InputError):
        pick_model(args("unknown/model"), "mbpp")


def test_read_task_never_raises(tmp_path):
    assert read_task(str(tmp_path / "missing.json"))[1].startswith("task file not found")
    (tmp_path / "bad.json").write_text("{not json")
    assert "cannot read" in read_task(str(tmp_path / "bad.json"))[1]
    (tmp_path / "ok.json").write_text('{"task_id": 1}')
    assert read_task(str(tmp_path / "ok.json")) == ({"task_id": 1}, None)


def test_missing_task_file_writes_a_solution_without_traceback(tmp_path):
    out = tmp_path / "solution.json"
    proc = subprocess.run([sys.executable, "-m", "agent_mbpp", "--task-file", str(tmp_path / "nope.json"),
                           "--output", str(out)], cwd=ROOT, capture_output=True, text=True, timeout=60)
    assert proc.returncode == 2 and "Traceback" not in proc.stderr
    assert json.loads(out.read_text())["error"].startswith("task file not found")


def test_run_tests_json_result():
    passed = json.loads(mbpp_tools.as_json(mbpp_tools.run_tests("def add(a, b):\n    return a + b",
                                                                ["assert add(1, 2) == 3", "assert add(0, 0) == 0"])))
    assert passed["success"] is True and (passed["passed"], passed["total"]) == (2, 2)
    assert passed["output"].startswith("ALL TESTS PASSED (2/2)")
    failed = json.loads(mbpp_tools.as_json(mbpp_tools.run_tests("def add(a, b):\n    return a - b",
                                                                ["assert add(1, 2) == 3", "assert add(0, 0) == 0"])))
    assert failed["success"] is False and (failed["passed"], failed["total"]) == (1, 2)
    assert "got:" in failed["output"]


def test_reaper_removes_the_container_once_the_agent_is_gone(tmp_path):
    """kill -9 runs no cleanup code in the agent: the reaper, a separate process, removes the container."""
    log = tmp_path / "docker.log"
    fake = tmp_path / "docker"
    fake.write_text(f'#!/bin/sh\necho "$@" >> "{log}"\n')
    fake.chmod(0o755)
    agent = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(30)"])
    env = dict(os.environ, PATH=f"{tmp_path}{os.pathsep}{os.environ['PATH']}")
    reaper = subprocess.Popen([sys.executable, str(ROOT / "src" / "agent_smith" / "container_reaper.py"),
                               str(agent.pid), "agent-smith-x-1"], env=env)
    try:
        assert reaper.poll() is None and not log.exists()  # agent alive: the container stays
        agent.kill()
        agent.wait()
        assert reaper.wait(timeout=10) == 0
        assert log.read_text().split() == ["rm", "-f", "agent-smith-x-1"]
    finally:
        agent.kill()
        reaper.kill()


def test_per_model_extra_body_wins_over_the_provider(monkeypatch):
    from agent_smith.llm import build_client
    from agent_smith.config import load_models_config
    cfg = load_models_config()
    monkeypatch.setenv("GROQ_API_KEY", "test")
    qwen = build_client("qwen/qwen3.8-27b", "https://api.groq.com/openai/v1", no_fallbacks=True, models_config=cfg)
    gpt = build_client("openai/gpt-oss-120b", "https://api.groq.com/openai/v1", no_fallbacks=True, models_config=cfg)
    assert qwen.endpoints[0].provider.extra_body["reasoning_effort"] == "none"   # thinking fills a 500-token reply
    assert gpt.endpoints[0].provider.extra_body["reasoning_effort"] == "low"     # the provider default
