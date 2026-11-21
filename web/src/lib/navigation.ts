/* Go to definition, peek, find references and back / forward, across the project's files.
 *
 * Monaco asks our providers (any language); the server searches declarations by name (studio/navigate.py).
 * The target files get a model (no tab) so peek / references can show them; opening a location goes through
 * the editors store (a real tab). Every jump records where it came from: Back / Forward walk that history. */
import type * as Mon from "monaco-editor";
import { codeApi, type CodeHit } from "./api";
import { editors } from "./editors";

type Monaco = typeof Mon;
interface Spot { path: string; line: number; col: number }

const back: Spot[] = [];
const fwd: Spot[] = [];
let installed = false;
let current: Mon.editor.IStandaloneCodeEditor | null = null;

const pathOf = (m: Mon.editor.ITextModel) => (m.uri.scheme === "file" ? m.uri.path.replace(/^\//, "") : null);
const here = (): Spot | null => {
  const m = current?.getModel(), p = m ? pathOf(m) : null, pos = current?.getPosition();
  return p && pos ? { path: p, line: pos.lineNumber, col: pos.column } : null;
};
const same = (a: Spot, b: Spot) => a.path === b.path && Math.abs(a.line - b.line) < 3;

function remember() {
  const s = here();
  if (s && !(back.length && same(back[back.length - 1]!, s))) { back.push(s); if (back.length > 100) back.shift(); }
  fwd.length = 0;
}

async function locations(monaco: Monaco, hits: CodeHit[]): Promise<Mon.languages.Location[]> {
  const out: Mon.languages.Location[] = [];
  for (const h of hits.slice(0, 60)) {
    const m = await editors.peekModel(h.path);  // peek / references need the text of the target
    if (!m) continue;
    out.push({ uri: m.uri, range: new monaco.Range(h.line, h.col, h.line, h.col + h.len) });
  }
  return out;
}

let walking = 0;  // while Back / Forward moves the cursor, that move is not a new history entry
function walk(to: Spot) { walking = Date.now(); void editors.open(to.path, { line: to.line, col: to.col }); }
export function goBack() { const s = back.pop(); if (!s) return; const h = here(); if (h) fwd.push(h); walk(s); }
export function goForward() { const s = fwd.pop(); if (!s) return; const h = here(); if (h) back.push(h); walk(s); }
export const canGo = () => ({ back: back.length > 0, forward: fwd.length > 0 });

/** Once per page, with the main code editor. */
export function installNavigation(monaco: Monaco, editor: Mon.editor.IStandaloneCodeEditor) {
  current = editor;
  if (installed) return;
  installed = true;
  // Monaco's own TS/JS service only knows the open files: its "definition" of an imported name is the import line
  // of the same file. Ours searches the project; keep the TS service for completion, hovers and errors.
  for (const d of [monaco.typescript.typescriptDefaults, monaco.typescript.javascriptDefaults]) {
    d.setModeConfiguration({ ...d.modeConfiguration, definitions: false, references: false });
  }
  const word = (model: Mon.editor.ITextModel, pos: Mon.Position) => model.getWordAtPosition(pos)?.word ?? "";

  monaco.languages.registerDefinitionProvider("*", {
    async provideDefinition(model, pos) {
      const name = word(model, pos);
      if (!/^[A-Za-z_$][\w$]*$/.test(name)) return null;
      const res = await codeApi.definition(name, pathOf(model) ?? undefined).catch(() => null);
      if (!res?.results.length) return null;
      // the declaration under the cursor itself: nothing to jump to (Monaco then offers references)
      const p = pathOf(model);
      const others = res.results.filter((h) => !(h.path === p && h.line === pos.lineNumber));
      return locations(monaco, others.length ? others : res.results);
    },
  });
  monaco.languages.registerReferenceProvider("*", {
    async provideReferences(model, pos) {
      const name = word(model, pos);
      if (!/^[A-Za-z_$][\w$]*$/.test(name)) return null;
      const res = await codeApi.references(name, pathOf(model) ?? undefined).catch(() => null);
      return res ? locations(monaco, res.results) : null;
    },
  });
  // a location in another file: open it in a tab (standalone Monaco cannot switch models by itself)
  monaco.editor.registerEditorOpener({
    openCodeEditor(_source, resource, sel) {
      if (resource.scheme !== "file") return false;
      const path = resource.path.replace(/^\//, "");
      const line = sel ? ("startLineNumber" in sel ? sel.startLineNumber : sel.lineNumber) : 1;
      const col = sel ? ("startColumn" in sel ? sel.startColumn : sel.column) : 1;
      remember();
      void editors.open(path, { line, col });
      return true;
    },
  });
  // jumps inside the same file are history too (like VS Code: a move of 10+ lines that is not typing)
  let prev: Spot | null = null;
  editor.onDidChangeCursorPosition((e) => {
    const s = here();
    if (s && prev && Date.now() - walking > 800 && s.path === prev.path && Math.abs(s.line - prev.line) >= 10 && e.source !== "keyboard"
        && !(back.length && same(back[back.length - 1]!, prev))) {
      back.push(prev); fwd.length = 0;
    }
    prev = s;
  });
  const mac = /Mac|iPhone|iPad/.test(navigator.platform);
  const { KeyMod, KeyCode } = monaco;
  editor.addAction({ id: "studio.back", label: "Go Back", keybindings: [mac ? KeyMod.WinCtrl | KeyCode.Minus : KeyMod.Alt | KeyCode.LeftArrow], run: goBack });
  editor.addAction({ id: "studio.forward", label: "Go Forward", keybindings: [mac ? KeyMod.WinCtrl | KeyMod.Shift | KeyCode.Minus : KeyMod.Alt | KeyCode.RightArrow], run: goForward });
}
