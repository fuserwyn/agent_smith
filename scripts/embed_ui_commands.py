"""Embed the default launcher commands into ui/index.html.

They are shown when the page is opened as a file (no server to ask). Same recipes as the server
(src/agent_smith/ui_jobs.py); tests/test_ui_server.py fails if the embedded copy drifts.

    uv run python scripts/embed_ui_commands.py
"""

import json
import re

from agent_smith.ui_jobs import ROOT, static_commands

UI = ROOT / "ui" / "index.html"
PATTERN = re.compile(r"/\*CMDS_START\*/.*?/\*CMDS_END\*/", re.S)


def embedded(text: str) -> dict:
    return json.loads(PATTERN.search(text).group(0)[len("/*CMDS_START*/"):-len("/*CMDS_END*/")])


def main() -> None:
    text = UI.read_text(encoding="utf-8")
    block = "/*CMDS_START*/" + json.dumps(static_commands(), ensure_ascii=False) + "/*CMDS_END*/"
    UI.write_text(PATTERN.sub(lambda _: block, text, count=1), encoding="utf-8")
    print(f"embedded default commands into {UI}")


if __name__ == "__main__":
    main()
