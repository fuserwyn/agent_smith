"""Code navigation for the editor: go to definition / find references across the project's files.

A search by name with per-language definition patterns (``def``, ``class``, ``function``, ``func``, ``struct``,
``fn`` …), not a type-aware analysis: the language servers (LSP) can replace it later behind the same API.
Results are ranked: the language of the file you are in first, then the same file, then the nearest folder.
"""

from __future__ import annotations

import re
from pathlib import PurePosixPath
from typing import Any, Dict, List, Optional

from agent_smith.studio.workspace import MAX_EDIT_BYTES, Workspace, WorkspaceError

NAME = re.compile(r"^[A-Za-z_$][\w$]{0,119}$")
MAX_RESULTS = 200

# extension -> language family
LANG = {
    **dict.fromkeys(["py", "pyi"], "python"),
    **dict.fromkeys(["js", "jsx", "mjs", "cjs", "ts", "tsx", "mts", "cts", "vue", "svelte"], "js"),
    "swift": "swift", "go": "go", "rs": "rust", "rb": "ruby", "php": "php", "dart": "dart", "lua": "lua",
    **dict.fromkeys(["java", "kt", "kts", "scala", "groovy", "cs"], "jvm"),
    **dict.fromkeys(["c", "h", "cc", "cpp", "cxx", "hpp", "hh", "hxx", "m", "mm"], "c"),
    **dict.fromkeys(["sh", "bash", "zsh"], "shell"),
}
# {n}: the escaped name. Strong patterns are declarations; weak ones (assignments) are used only when there is no strong one.
_STRONG = {
    "python": [r"^\s*(?:async\s+)?def\s+{n}\b", r"^\s*class\s+{n}\b"],
    "js": [r"\bfunction\s*\*?\s*{n}\b", r"\bclass\s+{n}\b", r"\b(?:interface|type|enum|namespace)\s+{n}\b",
           r"\b(?:const|let|var)\s+{n}\s*(?::[^=]+)?=\s*(?:async\s*)?(?:function\b|\([^)]*\)\s*(?::[^=]+)?=>|[\w$]+\s*=>)",
           r"^\s*(?:(?:public|private|protected|static|readonly|abstract|override|async|get|set)\s+)*\*?{n}\s*(?:<[^>]*>)?\s*\([^)]*\)\s*(?::\s*[^{{;]+)?\{{"],
    "swift": [r"\b(?:func|class|struct|enum|protocol|actor|typealias|extension|macro)\s+{n}\b", r"\bcase\s+{n}\b",
              r"^\s*(?:@\w+\s+)*(?:(?:public|private|fileprivate|internal|open|static|final|lazy|override)\s+)*(?:let|var)\s+{n}\b"],
    "go": [r"^\s*func\s+(?:\([^)]*\)\s*)?{n}\b", r"^\s*type\s+{n}\b", r"^\s*(?:const|var)\s+{n}\b", r"^\s+{n}\s+(?:=|[\w.*\[\]]+\s*=)"],
    "rust": [r"\b(?:fn|struct|enum|trait|type|mod|union|macro_rules!)\s+{n}\b", r"\b(?:const|static)\s+(?:mut\s+)?{n}\s*:"],
    "ruby": [r"^\s*def\s+(?:self\.)?{n}\b", r"^\s*(?:class|module)\s+{n}\b"],
    "php": [r"\bfunction\s+&?{n}\s*\(", r"\b(?:class|interface|trait|enum)\s+{n}\b"],
    "dart": [r"\b(?:class|mixin|enum|extension|typedef)\s+{n}\b", r"^\s*[\w<>?,\s]*\b{n}\s*\([^;]*\)\s*(?:async\s*)?\{{"],
    "lua": [r"\bfunction\s+(?:[\w.:]+[.:])?{n}\s*\(", r"\blocal\s+function\s+{n}\b"],
    "jvm": [r"\b(?:class|interface|enum|record|object|trait)\s+{n}\b", r"\bfun\s+(?:<[^>]*>\s*)?(?:[\w.]+\.)?{n}\s*\(",
            r"^\s*(?:(?:public|private|protected|internal|static|final|abstract|override|suspend|open|synchronized)\s+)*[\w<>\[\],.?\s]+\s+{n}\s*\([^;]*\)\s*(?:throws\s+[\w.,\s]+)?\{{?\s*$"],
    "c": [r"\b(?:struct|class|union|enum|namespace)\s+{n}\b", r"#\s*define\s+{n}\b",
          r"^[\w:*&<>,\s]*\b{n}\s*\([^;]*\)\s*(?:const\s*)?(?:noexcept\s*)?\{{?\s*$",
          r"^\s*[-+]\s*\([^)]*\)\s*{n}\b", r"@(?:interface|implementation|protocol)\s+{n}\b"],
    "shell": [r"^\s*(?:function\s+)?{n}\s*\(\s*\)"],
}
_WEAK = {
    "python": [r"^\s*{n}\s*(?::[^=]+)?=(?!=)"],
    "js": [r"\b(?:const|let|var)\s+{n}\b", r"^\s*(?:readonly\s+|static\s+|private\s+|public\s+)*{n}\s*[?!]?\s*[:=](?!=)"],
    "go": [r"\b{n}\s*:="],
    "rust": [r"\blet\s+(?:mut\s+)?{n}\b"],
}
_GENERIC = [r"\b(?:def|func|fn|function|fun|sub|class|struct|interface|enum|type|module)\s+{n}\b"]


def _family(path: str) -> Optional[str]:
    return LANG.get(PurePosixPath(path).suffix.lstrip(".").lower())


def _compile(patterns: List[str], name: str) -> List[re.Pattern]:
    n = re.escape(name)
    return [re.compile(p.format(n=n)) for p in patterns]


def _text(path, needle: bytes) -> Optional[str]:
    """The file's text if it is a text file that contains ``needle`` (read once)."""
    try:
        if path.stat().st_size > MAX_EDIT_BYTES:
            return None
        data = path.read_bytes()
    except OSError:
        return None
    if needle not in data or b"\x00" in data[:8192]:
        return None
    return data.decode("utf-8", errors="replace")


def _rank(here: Optional[str], fam: Optional[str]):
    here_dir = str(PurePosixPath(here).parent) if here else ""

    def key(hit: Dict[str, Any]):
        same_lang = 0 if fam and hit["lang"] == fam else 1
        same_file = 0 if hit["path"] == here else 1
        common = len(PurePosixPath(here_dir).parts) if here else 0
        p = PurePosixPath(hit["path"]).parent.parts
        shared = sum(1 for a, b in zip(p, PurePosixPath(here_dir).parts) if a == b) if here else 0
        test = 1 if re.search(r"(^|/)(tests?|spec|__tests__)/|_test\.|\.test\.|\.spec\.|test_", hit["path"]) else 0
        return (hit.get("weak", 0), same_lang, same_file, test, -shared, common, hit["path"], hit["line"])
    return key


def _check(name: str) -> str:
    name = (name or "").strip()
    if not NAME.match(name):
        raise WorkspaceError(400, "not an identifier")
    return name


def definitions(ws: Workspace, name: str, path: Optional[str] = None) -> Dict[str, Any]:
    """Where ``name`` is declared. ``path``: the file the user is in (ranking only)."""
    name = _check(name)
    fam = _family(path) if path else None
    needle = name.encode()
    strong: List[Dict[str, Any]] = []
    weak: List[Dict[str, Any]] = []
    cache: Dict[str, tuple] = {}
    for file in ws.walk_files():
        text = _text(file, needle)
        if text is None:
            continue
        rel = ws.rel(file)
        lang = _family(rel)
        key = lang or "*"
        if key not in cache:
            cache[key] = (_compile(_STRONG.get(lang, _GENERIC), name), _compile(_WEAK.get(lang, []), name))
        pats, wpats = cache[key]
        for no, line in enumerate(text.splitlines(), 1):
            if name not in line:
                continue
            col = None
            for p in pats:
                m = p.search(line)
                if m:
                    col = line.find(name, m.start()) + 1
                    strong.append({"path": rel, "line": no, "col": col, "len": len(name), "text": line.strip()[:240], "lang": lang})
                    break
            if col is None:
                for p in wpats:
                    m = p.search(line)
                    if m:
                        weak.append({"path": rel, "line": no, "col": line.find(name, m.start()) + 1, "len": len(name),
                                     "text": line.strip()[:240], "lang": lang, "weak": 1})
                        break
        if len(strong) >= MAX_RESULTS:
            break
    hits = strong if strong else weak
    # a weak hit in the same language family beats a strong one elsewhere only if nothing strong is in the family
    if fam and strong and not any(h["lang"] == fam for h in strong):
        hits = strong + [w for w in weak if w["lang"] == fam]
    hits.sort(key=_rank(path, fam))
    return {"name": name, "results": hits[:MAX_RESULTS], "truncated": len(hits) > MAX_RESULTS}


def references(ws: Workspace, name: str, path: Optional[str] = None) -> Dict[str, Any]:
    """Every whole-word use of ``name`` in files of the same language family (all text files if unknown)."""
    name = _check(name)
    fam = _family(path) if path else None
    word = re.compile(rf"(?<![\w$]){re.escape(name)}(?![\w$])")
    needle = name.encode()
    hits: List[Dict[str, Any]] = []
    for file in ws.walk_files():
        rel = ws.rel(file)
        lang = _family(rel)
        if fam and lang != fam:
            continue
        text = _text(file, needle)
        if text is None:
            continue
        for no, line in enumerate(text.splitlines(), 1):
            for m in word.finditer(line):
                hits.append({"path": rel, "line": no, "col": m.start() + 1, "len": len(name), "text": line.strip()[:240], "lang": lang})
            if len(hits) > 2000:
                break
        if len(hits) > 2000:
            break
    hits.sort(key=lambda h: (h["path"] != path, h["path"], h["line"], h["col"]))
    return {"name": name, "results": hits[:1000], "truncated": len(hits) > 1000}
