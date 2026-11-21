"""Claude Code as a chat engine: a fake `claude` CLI replays a stream-json session (no subscription needed)."""

import json
import os
import stat
import sys
import time

import pytest

pytest.importorskip("fastapi")
from fastapi.testclient import TestClient  # noqa: E402

from agent_smith.studio import claude_code  # noqa: E402
from agent_smith.studio.app import create_app  # noqa: E402

TOKEN = "cc-token"
H = {"Authorization": f"Bearer {TOKEN}", "X-Studio": "1"}

FAKE = r'''#!PYTHON
import json, os, sys
args = sys.argv[1:]
open(os.environ["FAKE_LOG"], "a").write(json.dumps({"args": args, "env": sorted(os.environ)}) + "\n")
say = lambda o: print(json.dumps(o), flush=True)
sid = args[args.index("--resume") + 1] if "--resume" in args else "sess-1"
say({"type": "system", "subtype": "init", "session_id": sid, "model": "claude-x", "permissionMode": args[args.index("--permission-mode") + 1]})
u = {"input_tokens": 3, "cache_read_input_tokens": 1000, "cache_creation_input_tokens": 200, "output_tokens": 20}
say({"type": "assistant", "message": {"id": "m1", "usage": u, "content": [{"type": "text", "text": "I will write it."}]}})
say({"type": "assistant", "message": {"id": "m1", "usage": u, "content": [{"type": "tool_use", "id": "t1", "name": "Write", "input": {"file_path": "hello.py", "content": "print(1)"}}]}})
if args[args.index("--permission-mode") + 1] != "plan":
    open("hello.py", "w").write("print(1)\n")
say({"type": "user", "message": {"content": [{"type": "tool_result", "tool_use_id": "t1", "content": "File created"}]}})
say({"type": "assistant", "message": {"id": "m2", "usage": u, "content": [{"type": "text", "text": "Done."}]}})
say({"type": "result", "subtype": "success", "is_error": False, "result": "Created hello.py.", "session_id": sid,
     "total_cost_usd": 0.0123, "usage": {"input_tokens": 6, "cache_read_input_tokens": 2000, "cache_creation_input_tokens": 400, "output_tokens": 40}})
'''


@pytest.fixture()
def studio(tmp_path, monkeypatch):
    fake = tmp_path / "bin" / "claude"
    fake.parent.mkdir()
    fake.write_text(FAKE.replace("PYTHON", sys.executable))
    fake.chmod(fake.stat().st_mode | stat.S_IEXEC)
    log = tmp_path / "calls.jsonl"
    monkeypatch.setattr(claude_code, "binary", lambda: str(fake))
    monkeypatch.setenv("CLAUDE_CODE_OAUTH_TOKEN", "oauth-test")
    monkeypatch.setenv("OPENROUTER_API_KEY", "sk-or-secret")
    monkeypatch.setenv("FAKE_LOG", str(log))
    monkeypatch.setattr(claude_code, "PASS_ENV", claude_code.PASS_ENV + ("FAKE_LOG",))
    project = tmp_path / "proj"
    project.mkdir()
    (project / "notes.md").write_text("mine\n")
    app = create_app(project, TOKEN, web_dist=None, shell="local", state_dir=tmp_path / "state")
    return TestClient(app, base_url="http://127.0.0.1:8790"), project, log


def wait(client, rid):
    deadline = time.monotonic() + 30
    while client.get("/api/agent/state", headers=H).json()["runs"][rid]["status"] == "running":
        assert time.monotonic() < deadline
        time.sleep(0.05)
    return client.get("/api/agent/state", headers=H).json()["runs"][rid]


def test_claude_code_is_a_tier_of_its_own(studio):
    client, _, _ = studio
    with client:
        models = client.get("/api/agent/models", headers=H).json()
        cc = [m for m in models["models"] if m["provider"] == "claude-code"]
        assert {m["model"] for m in cc} >= {"sonnet", "opus", "haiku"} and all(m["tier"] == "claude" and m["key_ok"] for m in cc)
        assert models["tiers"]["claude"] is True


def test_a_claude_code_turn_streams_steps_lists_its_changes_and_resumes(studio):
    client, project, log = studio
    with client:
        rid = client.post("/api/agent/runs", json={"message": "write hello", "mode": "agent", "provider": "claude-code", "model": "sonnet"}, headers=H).json()["id"]
        run = wait(client, rid)
        assert run["status"] == "done" and run["answer"] == "Created hello.py." and run["tier"] == "claude"
        assert run["steps"] == 2                                          # one reply split in two events = one step
        assert (run["input_tokens"], run["output_tokens"], run["cost_usd"]) == (2406, 40, 0.0123)
        assert [c["path"] for c in run["changes"]] == ["hello.py"]        # its own write, not notes.md
        call = json.loads(log.read_text().splitlines()[0])
        args = call["args"]
        assert args[args.index("--permission-mode") + 1] == "auto" and args[args.index("--model") + 1] == "sonnet"
        assert "CLAUDE_CODE_OAUTH_TOKEN" in call["env"] and "OPENROUTER_API_KEY" not in call["env"]  # only its token
        trace = client.get(f"/api/agent/runs/{rid}/trace", headers=H).text
        assert '"name": "Write"' in trace and '"filepath": "hello.py"' in trace
        # Ask = plan mode, and the chat continues the same session
        rid2 = client.post("/api/agent/runs", json={"message": "and now?", "mode": "ask", "provider": "claude-code", "model": "haiku"}, headers=H).json()["id"]
        assert wait(client, rid2)["status"] == "done"
        args2 = json.loads(log.read_text().splitlines()[1])["args"]
        assert args2[args2.index("--permission-mode") + 1] == "plan" and args2[args2.index("--resume") + 1] == "sess-1"
        assert args2[args2.index("-p") + 1] == "and now?"                  # the session has the history already


def test_without_a_token_the_tier_is_explained(studio, monkeypatch):
    client, _, _ = studio
    monkeypatch.delenv("CLAUDE_CODE_OAUTH_TOKEN")
    with client:
        r = client.post("/api/agent/runs", json={"message": "x", "mode": "agent", "provider": "claude-code", "model": "sonnet"}, headers=H)
        assert r.status_code == 400 and "CLAUDE_CODE_OAUTH_TOKEN" in r.json()["error"]
