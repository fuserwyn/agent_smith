"""Go to definition / find references across files (studio/navigate.py)."""

import pytest

from agent_smith.studio import navigate
from agent_smith.studio.workspace import Workspace, WorkspaceError


@pytest.fixture()
def ws(tmp_path):
    files = {
        "app/models.py": "class Order:\n    pass\n\n\ndef total(order):\n    return 1\n",
        "app/views.py": "from app.models import Order, total\n\n\ndef show(o: Order):\n    return total(o)\n",
        "tests/test_models.py": "def total():\n    return 0\n",
        "web/cart.ts": "export interface Cart { items: string[] }\nexport const total = (c: Cart) => c.items.length;\n",
        "web/page.tsx": "import { total } from './cart';\nexport function Page() { return total({ items: [] }); }\n",
        "ios/Store.swift": "struct Store {\n    func total() -> Int { 0 }\n}\n",
        "srv/main.go": "package main\n\nfunc total() int { return 0 }\n",
        "node_modules/x/index.js": "function total() {}\n",  # heavy folder: never searched
    }
    for p, text in files.items():
        f = tmp_path / p
        f.parent.mkdir(parents=True, exist_ok=True)
        f.write_text(text)
    return Workspace(tmp_path)


def test_definition_prefers_the_language_and_the_code_over_tests(ws):
    hits = navigate.definitions(ws, "total", "app/views.py")["results"]
    assert (hits[0]["path"], hits[0]["line"]) == ("app/models.py", 5)        # same language, not the test file
    assert {h["path"] for h in hits} >= {"web/cart.ts", "ios/Store.swift", "srv/main.go"}
    assert not any(h["path"].startswith("node_modules") for h in hits)
    ts = navigate.definitions(ws, "total", "web/page.tsx")["results"]
    assert (ts[0]["path"], ts[0]["line"]) == ("web/cart.ts", 2)               # const arrow function
    assert navigate.definitions(ws, "Cart", "web/page.tsx")["results"][0]["path"] == "web/cart.ts"
    assert navigate.definitions(ws, "Store", "ios/Store.swift")["results"][0]["line"] == 1
    cls = navigate.definitions(ws, "Order", "app/views.py")["results"]
    assert [(h["path"], h["line"], h["col"]) for h in cls] == [("app/models.py", 1, 7)]  # not the import / annotation


def test_references_stay_in_the_language_and_match_whole_words(ws):
    refs = navigate.references(ws, "total", "app/views.py")["results"]
    assert {h["path"] for h in refs} == {"app/models.py", "app/views.py", "tests/test_models.py"}
    assert refs[0]["path"] == "app/views.py"                                   # the file you are in first


def test_only_identifiers_are_searched(ws):
    for bad in ("", "a b", "x;rm", "../etc", "1abc"):
        with pytest.raises(WorkspaceError):
            navigate.definitions(ws, bad)
