"""Studio project access: the jail (no way out of the project), read/write with conflicts, search, git."""

import os
import subprocess
import time

import pytest

pytest.importorskip("fastapi")
from fastapi.testclient import TestClient  # noqa: E402

from agent_smith.studio.app import create_app  # noqa: E402
from agent_smith.studio.workspace import MAX_EDIT_BYTES, Workspace, WorkspaceError  # noqa: E402

TOKEN = "t0k"
H = {"Authorization": f"Bearer {TOKEN}"}
W = {**H, "X-Studio": "1"}


@pytest.fixture
def proj(tmp_path):
    root = tmp_path / "proj"
    (root / "src").mkdir(parents=True)
    (root / "src" / "main.py").write_text("def add(a, b):\n    return a + b\n")
    (root / "README.md").write_text("# Demo\nHello world\n")
    (root / "node_modules" / "x").mkdir(parents=True)
    (root / "node_modules" / "x" / "index.js").write_text("hello inside heavy\n")
    (root / ".env").write_text("SECRET=1\n")
    (root / ".git").mkdir()
    (tmp_path / "outside.txt").write_text("top secret\n")
    return root


@pytest.fixture
def client(proj):
    return TestClient(create_app(proj, TOKEN, web_dist=None), base_url="http://127.0.0.1:8790")


# ---------------------------------------------------------------- jail
@pytest.mark.parametrize("bad", ["../outside.txt", "src/../../outside.txt", "/etc/passwd", "C:/Windows/win.ini",
                                 "..\\outside.txt", "src/\x00.py"])
def test_paths_cannot_leave_the_project(proj, bad):
    with pytest.raises(WorkspaceError) as e:
        Workspace(proj).resolve(bad)
    assert e.value.status in (400, 403)


def test_symlink_out_of_the_project_is_refused(proj, tmp_path, client):
    os.symlink(tmp_path / "outside.txt", proj / "link.txt")
    os.symlink(tmp_path, proj / "linkdir")
    for p in ("link.txt", "linkdir/outside.txt"):
        assert client.get("/api/fs/read", params={"path": p}, headers=H).status_code == 403
    r = client.put("/api/fs/write", json={"path": "linkdir/new.txt", "content": "x"}, headers=W)
    assert r.status_code == 403 and not (tmp_path / "new.txt").exists()
    entries = {e["name"]: e for e in client.get("/api/fs/list", headers=H).json()["entries"]}
    assert entries["link.txt"]["symlink"] and entries["link.txt"]["outside"]


def test_symlink_inside_the_project_works(proj, client):
    os.symlink(proj / "src" / "main.py", proj / "alias.py")
    assert "def add" in client.get("/api/fs/read", params={"path": "alias.py"}, headers=H).json()["content"]


def test_api_traversal_attempts(client):
    for p in ("../outside.txt", "/etc/hosts", "%2e%2e/outside.txt"):
        assert client.get("/api/fs/read", params={"path": p}, headers=H).status_code in (400, 403, 404)
    assert client.post("/api/fs/delete", json={"path": ""}, headers=W).status_code == 400  # the root itself
    assert client.post("/api/fs/rename", json={"from": "README.md", "to": "../x.md"}, headers=W).status_code == 400


def test_file_api_needs_token_and_csrf_header(client):
    assert client.get("/api/fs/list").status_code == 401
    assert client.put("/api/fs/write", json={"path": "a.txt", "content": "x"}, headers=H).status_code == 403


# ---------------------------------------------------------------- listing and reading
def test_list_sorts_folders_first_and_flags_heavy_and_secrets(client):
    entries = client.get("/api/fs/list", headers=H).json()["entries"]
    names = [e["name"] for e in entries]
    assert names.index("src") < names.index("README.md")
    by = {e["name"]: e for e in entries}
    assert ".git" not in by  # hidden like in VS Code
    assert by["node_modules"]["heavy"] and by[".env"]["sensitive"] and not by["README.md"]["sensitive"]


def test_read_text_binary_and_large(proj, client):
    f = client.get("/api/fs/read", params={"path": "src/main.py"}, headers=H).json()
    assert f["content"].startswith("def add") and f["hash"] and not f["readonly"] and f["eol"] == "lf"
    (proj / "img.bin").write_bytes(b"\x89PNG\x00\x00\x01")
    b = client.get("/api/fs/read", params={"path": "img.bin"}, headers=H).json()
    assert b["binary"] and b["content"] is None and b["readonly"]
    (proj / "big.log").write_text("x" * (MAX_EDIT_BYTES + 10))
    big = client.get("/api/fs/read", params={"path": "big.log"}, headers=H).json()
    assert big["too_large"] and big["readonly"] and len(big["content"]) < MAX_EDIT_BYTES
    assert client.get("/api/fs/read", params={"path": "src"}, headers=H).status_code == 400
    assert client.get("/api/fs/read", params={"path": "nope.py"}, headers=H).status_code == 404


# ---------------------------------------------------------------- writing
def test_write_detects_conflicts(proj, client):
    f = client.get("/api/fs/read", params={"path": "src/main.py"}, headers=H).json()
    (proj / "src" / "main.py").write_text("changed by the agent\n")  # someone else wrote meanwhile
    r = client.put("/api/fs/write", json={"path": "src/main.py", "content": "mine", "base_hash": f["hash"]}, headers=W)
    assert r.status_code == 409 and (proj / "src" / "main.py").read_text() == "changed by the agent\n"
    fresh = client.get("/api/fs/read", params={"path": "src/main.py"}, headers=H).json()
    ok = client.put("/api/fs/write", json={"path": "src/main.py", "content": "mine\n", "base_hash": fresh["hash"]}, headers=W)
    assert ok.status_code == 200 and (proj / "src" / "main.py").read_text() == "mine\n"
    assert ok.json()["hash"] == client.get("/api/fs/read", params={"path": "src/main.py"}, headers=H).json()["hash"]


def test_write_keeps_mode_and_leaves_no_temp_files(proj, client):
    script = proj / "run.sh"
    script.write_text("echo hi\n")
    script.chmod(0o755)
    client.put("/api/fs/write", json={"path": "run.sh", "content": "echo bye\n"}, headers=W)
    assert os.stat(script).st_mode & 0o777 == 0o755
    assert not [p for p in proj.iterdir() if p.name.startswith(".studio-")]


def test_create_rename_delete(proj, client):
    assert client.post("/api/fs/create", json={"path": "pkg/new.py", "content": "x = 1\n"}, headers=W).status_code == 200
    assert (proj / "pkg" / "new.py").read_text() == "x = 1\n"
    assert client.post("/api/fs/create", json={"path": "pkg/new.py"}, headers=W).status_code == 409
    assert client.post("/api/fs/create", json={"path": "docs", "kind": "dir"}, headers=W).json()["type"] == "dir"
    assert client.post("/api/fs/rename", json={"from": "pkg/new.py", "to": "pkg/renamed.py"}, headers=W).status_code == 200
    assert (proj / "pkg" / "renamed.py").exists() and not (proj / "pkg" / "new.py").exists()
    assert client.post("/api/fs/rename", json={"from": "README.md", "to": "src/main.py"}, headers=W).status_code == 409
    assert client.post("/api/fs/delete", json={"path": "pkg"}, headers=W).status_code == 200 and not (proj / "pkg").exists()


def test_unicode_and_spaces_in_names(proj, client):
    r = client.post("/api/fs/create", json={"path": "мой каталог/файл с пробелом.txt", "content": "привет\n"}, headers=W)
    assert r.status_code == 200
    assert client.get("/api/fs/read", params={"path": "мой каталог/файл с пробелом.txt"}, headers=H).json()["content"] == "привет\n"


# ---------------------------------------------------------------- search
def test_search_skips_heavy_folders_and_binary(proj, client):
    (proj / "blob.bin").write_bytes(b"hello\x00world")
    res = client.get("/api/fs/search", params={"q": "hello"}, headers=H).json()
    assert [r["path"] for r in res["results"]] == ["README.md"] and res["results"][0]["line"] == 2
    assert client.get("/api/fs/search", params={"q": "HELLO", "case": True}, headers=H).json()["results"] == []
    assert client.get("/api/fs/search", params={"q": r"def \w+\(", "regex": True}, headers=H).json()["results"][0]["path"] == "src/main.py"
    assert client.get("/api/fs/search", params={"q": "(", "regex": True}, headers=H).status_code == 400


def test_quick_open_file_index(client):
    files = client.get("/api/fs/files", headers=H).json()["files"]
    assert "src/main.py" in files and "README.md" in files and not any(f.startswith("node_modules/") for f in files)


# ---------------------------------------------------------------- git
def _git(root, *args):
    subprocess.run(["git", "-C", str(root), *args], check=True, capture_output=True,
                   env={**os.environ, "GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@t", "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@t"})


def test_git_status_and_diff(proj, client):
    assert client.get("/api/git/status", headers=H).json() == {"repo": False, "branch": None, "files": []}
    _git(proj, "init", "-q", "-b", "main")
    _git(proj, "add", "src/main.py", "README.md")
    _git(proj, "commit", "-q", "-m", "init")
    (proj / "src" / "main.py").write_text("def add(a, b):\n    return b + a\n")
    (proj / "notes.txt").write_text("new\n")
    (proj / "README.md").unlink()
    st = client.get("/api/git/status", headers=H).json()
    assert st["repo"] and st["branch"] == "main"
    by = {f["path"]: f["status"] for f in st["files"]}
    assert by["src/main.py"] == "modified" and by["notes.txt"] == "untracked" and by["README.md"] == "deleted"
    d = client.get("/api/git/diff", params={"path": "src/main.py"}, headers=H).json()
    assert "a + b" in d["original"] and "b + a" in d["modified"] and not d["added"]
    assert client.get("/api/git/diff", params={"path": "notes.txt"}, headers=H).json()["added"]
    assert client.get("/api/git/diff", params={"path": "README.md"}, headers=H).json()["deleted"]


# ---------------------------------------------------------------- live events
def test_file_change_events_reach_the_browser(proj, client):
    with client.websocket_connect(f"ws://127.0.0.1:8790/ws/events?token={TOKEN}") as ws:
        assert ws.receive_json()["type"] == "hello"
        time.sleep(0.3)  # watcher started
        (proj / "src" / "fresh.py").write_text("x = 1\n")
        seen = []
        deadline = time.monotonic() + 10
        while time.monotonic() < deadline and not any(c["path"] == "src/fresh.py" for c in seen):
            msg = ws.receive_json()
            if msg["type"] == "fs":
                seen += msg["changes"]
        assert any(c["path"] == "src/fresh.py" for c in seen)
        assert not any(c["path"].startswith(".git") for c in seen)  # hidden folders produce no events
