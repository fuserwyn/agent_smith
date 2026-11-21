"""Checkpoints of the project before each agent turn, in a *shadow* git repository.

The shadow repository lives in the studio's state folder (never inside the project) and uses the
project folder as its work tree, so the user's own ``.git`` (branches, index, stash) is never touched.
The project's ``.gitignore`` is honoured and heavy folders are excluded. After a turn the studio lists
what the agent changed, shows each diff, and can restore one file or the whole turn.
"""

from __future__ import annotations

import os
import subprocess
from pathlib import Path
from typing import Any, Dict, List, Optional

from agent_smith.studio.workspace import HEAVY_DIRS, Workspace, WorkspaceError

STATUS = {"A": "added", "M": "modified", "D": "deleted", "T": "modified"}


class Checkpoints:
    def __init__(self, ws: Workspace, state_dir: Path):
        self.ws = ws
        self.git_dir = Path(state_dir) / "shadow.git"

    def _git(self, *args: str, check: bool = True, timeout: float = 120) -> subprocess.CompletedProcess:
        env = {**os.environ, "GIT_DIR": str(self.git_dir), "GIT_WORK_TREE": str(self.ws.root), "GIT_OPTIONAL_LOCKS": "0",
               "GIT_TERMINAL_PROMPT": "0", "GIT_AUTHOR_NAME": "agent-smith-studio", "GIT_AUTHOR_EMAIL": "studio@localhost",
               "GIT_COMMITTER_NAME": "agent-smith-studio", "GIT_COMMITTER_EMAIL": "studio@localhost", "LC_ALL": "C"}
        r = subprocess.run(["git", "-c", "core.autocrlf=false", "-c", "core.quotepath=false", "-c", "commit.gpgsign=false",
                            *args], cwd=self.ws.root, env=env, capture_output=True, timeout=timeout)
        if check and r.returncode != 0:
            raise WorkspaceError(500, "checkpoint failed: " + r.stderr.decode(errors="replace").strip()[:300])
        return r

    def _init(self) -> None:
        if (self.git_dir / "HEAD").exists():
            return
        self.git_dir.mkdir(parents=True, exist_ok=True)
        self._git("init", "-q")
        exclude = self.git_dir / "info" / "exclude"
        exclude.parent.mkdir(parents=True, exist_ok=True)
        exclude.write_text("".join(f"{d}/\n" for d in sorted(HEAVY_DIRS)) + ".DS_Store\n")

    def snapshot(self, label: str) -> str:
        """Commit the whole project as it is now; returns the commit id."""
        self._init()
        self._git("add", "-A", ".")
        self._git("commit", "-q", "--allow-empty", "--no-verify", "-m", label)
        return self._git("rev-parse", "HEAD").stdout.decode().strip()

    def tree(self) -> str:
        """The project as it is now, as a git tree id (no commit): cheap marks around each agent action."""
        self._init()
        self._git("add", "-A", ".")
        return self._git("write-tree").stdout.decode().strip()

    def changed_between(self, a: str, b: str) -> List[str]:
        """Paths that differ between two trees / commits."""
        if a == b:
            return []
        r = self._git("diff-tree", "-r", "--name-only", "--no-renames", "-z", a, b)
        return [p for p in r.stdout.decode("utf-8", errors="replace").split("\x00") if p]

    def changes(self, since: str, only: Optional[List[str]] = None) -> List[Dict[str, Any]]:
        """Files that differ between the checkpoint and the project now (added / modified / deleted);
        ``only``: restricted to these paths (the ones the agent's own actions changed)."""
        self._init()
        self._git("add", "-A", ".")  # stage everything (untracked included) into the shadow index
        r = self._git("diff", "--cached", "--name-status", "--no-renames", "-z", since)
        parts = [p for p in r.stdout.decode("utf-8", errors="replace").split("\x00") if p]
        out = []
        keep = set(only) if only is not None else None
        for code, path in zip(parts[0::2], parts[1::2]):
            if keep is None or path in keep:
                out.append({"path": path, "status": STATUS.get(code[0], "modified")})
        return sorted(out, key=lambda c: c["path"])

    def original(self, since: str, rel: str) -> Optional[str]:
        """The file as it was at the checkpoint (None if it did not exist)."""
        self.ws.resolve(rel, must_exist=False)  # jail check
        r = self._git("show", f"{since}:{rel}", check=False)
        if r.returncode != 0:
            return None
        if b"\x00" in r.stdout[:8192]:
            return None
        return r.stdout.decode("utf-8", errors="replace")

    def restore(self, since: str, paths: Optional[List[str]] = None, only: Optional[List[str]] = None) -> List[str]:
        """Put files back as they were at the checkpoint; files created since are removed.
        ``only``: the files the agent changed (never touch what someone else changed meanwhile)."""
        changed = {c["path"]: c["status"] for c in self.changes(since, only)}
        targets = [p for p in (paths if paths is not None else list(changed)) if p in changed]
        restored = []
        for rel in targets:
            path = self.ws.resolve(rel, must_exist=False)
            if changed[rel] == "added":
                if path.exists() or path.is_symlink():
                    path.unlink()
            else:
                self._git("checkout", since, "--", rel)
            restored.append(rel)
        return restored
