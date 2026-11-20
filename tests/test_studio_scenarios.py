"""run_agent_smith: the chat agent starts Agent Smith's own fixed recipe; the inner trace is served live."""

import json
import sys
import time

import pytest

pytest.importorskip("fastapi")
from fastapi.testclient import TestClient  # noqa: E402

from agent_smith import ui_jobs  # noqa: E402
from agent_smith.studio.agent import AgentService  # noqa: E402
from agent_smith.studio.app import create_app  # noqa: E402
from tests.fake_llm_server import FakeLLMServer  # noqa: E402

TOKEN = "scn-token"
H = {"Authorization": f"Bearer {TOKEN}", "X-Studio": "1"}
INNER = ("import json,sys,pathlib; d=pathlib.Path(sys.argv[1]); d.mkdir(parents=True, exist_ok=True); "
         "(d/'events.jsonl').write_text(json.dumps({'type':'run_start','benchmark':'mbpp','task_id':'11'})+'\\n'"
         "+json.dumps({'type':'run_end','success':True})+'\\n'); "
         "(d/'solution.json').write_text(json.dumps({'success':True,'iterations':2,'total_input_tokens':10,"
         "'total_output_tokens':5,'total_time_seconds':1.0,'solution':'def f(): pass'})); print('Overall: PASSED')")


@pytest.fixture
def fake_repo(tmp_path, monkeypatch):
    """Agent Smith's paths pointed at a temp folder; the recipe replaced by a tiny inner 'agent'."""
    root = tmp_path / "agent_smith"
    (root / "moulinette").mkdir(parents=True)
    (root / "moulinette" / "pyproject.toml").write_text("")
    monkeypatch.setattr(ui_jobs, "ROOT", root)
    monkeypatch.setattr(ui_jobs, "MOULINETTE", root / "moulinette")
    monkeypatch.setattr(ui_jobs, "JOBS_DIR", root / "runs" / "ui")
    seen = []

    def plan(action, params, job_dir):
        seen.append((action, params))
        if params.get("task_id") == "bad":
            raise ui_jobs.ActionError("MBPP task id must be a number")
        return [ui_jobs.Step("step_agent", [sys.executable, "-c", INNER, str(job_dir)], root)]

    monkeypatch.setattr(ui_jobs, "plan", plan)
    return seen


def make_client(tmp_path, monkeypatch, script):
    project = tmp_path / "proj"
    project.mkdir()
    srv = FakeLLMServer(script)
    srv.__enter__()
    monkeypatch.setattr(AgentService, "models", staticmethod(lambda: {
        "models": [{"provider": "fake", "model": "fake-model", "url": srv.url, "key_ok": True, "paid": False, "key_env": None, "notes": ""}],
        "default": {"provider": "fake", "model": "fake-model"}}))
    app = create_app(project, TOKEN, web_dist=None, shell="local", state_dir=tmp_path / "state")
    return TestClient(app, base_url="http://127.0.0.1:8790"), srv


def wait_done(client, run_id):
    deadline = time.monotonic() + 60
    while time.monotonic() < deadline:
        run = client.get("/api/agent/state", headers=H).json()["runs"][run_id]
        if run["status"] != "running":
            return run
        time.sleep(0.1)
    pytest.fail("run did not finish")


def step(code):
    return f"Thought: next.\n```python\n{code}\n```"


def test_the_chat_agent_runs_agent_smith_and_sees_the_verdict(tmp_path, monkeypatch, fake_repo):
    client, srv = make_client(tmp_path, monkeypatch, [
        step("print(run_agent_smith('mbpp', '11'))"),
        step("final_answer('MBPP task 11 passed.')"),
    ])
    with client, srv:
        run = client.post("/api/agent/runs", json={"message": "run mbpp 11", "mode": "agent"}, headers=H).json()
        done = wait_done(client, run["id"])
        assert done["status"] == "done", done
        system = srv.requests[0]["messages"][0]["content"]
        assert "run_agent_smith(" in system                               # offered like an MCP tool
        observation = srv.requests[1]["messages"][-1]["content"]
        assert "verdict PASSED" in observation and "iterations=2" in observation and "def f(): pass" in observation
        assert fake_repo == [("mbpp", {"task_id": "11"})]
        [job] = client.get("/api/scenarios", headers=H).json()["jobs"]
        assert job["run"] == run["id"] and job["status"] == "done" and job["verdict"] == "PASSED"
        trace = client.get(f"/api/scenarios/{job['id']}/trace", headers=H).json()["events"]
        assert [e["type"] for e in trace] == ["run_start", "run_end"]
        assert client.get(f"/api/scenarios/{job['id']}/trace", params={"since": 1}, headers=H).json()["events"][0]["type"] == "run_end"
        assert client.get("/api/scenarios/nope/trace", headers=H).status_code == 404
        events = [json.loads(line) for line in (tmp_path / "state" / "runs" / f"{run['id']}.jsonl").read_text().splitlines()]
        calls = [e for e in events if e["type"] == "tool_call" and e.get("name") == "run_agent_smith"]
        assert calls, "the call goes through the sandbox's tool proxy like any MCP tool"


def test_not_offered_in_ask_mode_and_bad_arguments_are_reported(tmp_path, monkeypatch, fake_repo):
    client, srv = make_client(tmp_path, monkeypatch, [
        step("print(run_agent_smith('tetris'))"),
        step("print(run_agent_smith('mbpp', 'bad'))"),
        step("print(run_agent_smith('mbpp', model='nope'))"),
        step("final_answer('ok')"),
        step("final_answer('ok')"),
    ])
    with client, srv:
        run = client.post("/api/agent/runs", json={"message": "x", "mode": "agent"}, headers=H).json()
        wait_done(client, run["id"])
        obs = [srv.requests[i]["messages"][-1]["content"] for i in (1, 2, 3)]
        assert "ToolError: benchmark must be 'mbpp' or 'swebench'" in obs[0]   # errors reach the model as ToolError
        assert "must be a number" in obs[1] and "unknown model 'nope'" in obs[2]
        run = client.post("/api/agent/runs", json={"message": "y", "mode": "ask"}, headers=H).json()
        wait_done(client, run["id"])
        assert "run_agent_smith" not in srv.requests[-1]["messages"][0]["content"]
