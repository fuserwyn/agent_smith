"""Read-only git information for the studio: branch, changed files, file at HEAD (for the diff view)."""

from __future__ import annotations

import os
import shutil
import subprocess
from pathlib import Path
from typing import Any, Dict, List, Optional

from agent_smith.studio.workspace import Workspace, WorkspaceError

GIT_ENV = {**os.environ, "GIT_OPTIONAL_LOCKS": "0", "GIT_TERMINAL_PROMPT": "0", "LC_ALL": "C"}
LABEL = {"M": "modified", "A": "added", "D": "deleted", "R": "renamed", "C": "copied", "U": "conflict", "?": "untracked", "T": "modified"}


def _git(root: Path, *args: str, timeout: float = 10) -> subprocess.CompletedProcess:
    return subprocess.run(["git", "-C", str(root), *args], capture_output=True, timeout=timeout, env=GIT_ENV)


def is_repo(root: Path) -> bool:
    if not shutil.which("git"):
        return False
    try:
        r = _git(root, "rev-parse", "--show-toplevel")
    except (OSError, subprocess.TimeoutExpired):
        return False
    return r.returncode == 0


def status(ws: Workspace) -> Dict[str, Any]:
    if not is_repo(ws.root):
        return {"repo": False, "branch": None, "files": []}
    top = Path(_git(ws.root, "rev-parse", "--show-toplevel").stdout.decode().strip()).resolve()
    r = _git(ws.root, "status", "--porcelain=v1", "-b", "-z", "--untracked-files=all", "--", ".")
    if r.returncode != 0:
        raise WorkspaceError(500, r.stderr.decode(errors="replace").strip() or "git status failed")
    items = r.stdout.decode("utf-8", errors="replace").split("\x00")
    branch: Optional[str] = None
    ahead = behind = 0
    files: List[Dict[str, Any]] = []
    i = 0
    while i < len(items):
        rec = items[i]
        i += 1
        if not rec:
            continue
        if rec.startswith("## "):
            head = rec[3:]
            branch = head.split("...")[0].replace("No commits yet on ", "")
            if "[" in head:
                info = head[head.index("[") + 1:head.rindex("]")]
                for part in info.split(", "):
                    if part.startswith("ahead "):
                        ahead = int(part[6:])
                    elif part.startswith("behind "):
                        behind = int(part[7:])
            continue
        code, path = rec[:2], rec[3:]
        old = None
        if code[0] in "RC":  # renames carry the old path as the next record
            old = items[i]
            i += 1
        abs_path = (top / path).resolve()
        if ws.root != abs_path and ws.root not in abs_path.parents:
            continue  # outside the opened folder (opened a sub-folder of a repo)
        key = "?" if code == "??" else ("U" if "U" in code else (code[0] if code[0] != " " else code[1]))
        files.append({"path": ws.rel(abs_path), "code": code, "status": LABEL.get(key, "modified"),
                      "staged": code[0] not in " ?", "old": old})
    files.sort(key=lambda f: f["path"])
    return {"repo": True, "branch": branch, "ahead": ahead, "behind": behind, "files": files}


def head_content(ws: Workspace, rel: str) -> Optional[str]:
    """The file as committed at HEAD (None when it is new or not in a repo)."""
    path = ws.resolve(rel, must_exist=False)
    if not is_repo(ws.root):
        return None
    r = _git(ws.root, "show", f"HEAD:./{ws.rel(path)}")
    if r.returncode != 0:
        return None
    data = r.stdout
    if b"\x00" in data[:8192]:
        return None
    return data.decode("utf-8", errors="replace")


def diff(ws: Workspace, rel: str) -> Dict[str, Any]:
    path = ws.resolve(rel, must_exist=False)
    original = head_content(ws, rel)
    modified = None
    if path.exists() and path.is_file():
        f = ws.read(rel)
        if f["binary"] or f["too_large"]:
            raise WorkspaceError(415, "binary or too large for a diff")
        modified = f["content"]
    return {"path": ws.rel(path), "original": original or "", "modified": modified or "",
            "added": original is None, "deleted": modified is None}
