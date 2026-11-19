/* Monaco (the VS Code editor), bundled locally: no CDN, works offline. Loaded lazily (big chunk). */
import * as monaco from "monaco-editor";
import EditorWorker from "monaco-editor/editor/editor.worker?worker";
import JsonWorker from "monaco-editor/language/json/json.worker?worker";
import CssWorker from "monaco-editor/language/css/css.worker?worker";
import HtmlWorker from "monaco-editor/language/html/html.worker?worker";
import TsWorker from "monaco-editor/language/typescript/ts.worker?worker";

self.MonacoEnvironment = {
  getWorker(_id: string, label: string) {
    if (label === "json") return new JsonWorker();
    if (label === "css" || label === "scss" || label === "less") return new CssWorker();
    if (label === "html" || label === "handlebars" || label === "razor") return new HtmlWorker();
    if (label === "typescript" || label === "javascript") return new TsWorker();
    return new EditorWorker();
  },
};

const common = {
  "editorLineNumber.foreground": "#4b5468",
  "editorIndentGuide.background1": "#00000000",
};
monaco.editor.defineTheme("studio-dark", {
  base: "vs-dark", inherit: true, rules: [],
  colors: {
    ...common,
    "editor.background": "#0f1218",
    "editor.lineHighlightBackground": "#151922",
    "editor.lineHighlightBorder": "#00000000",
    "editorLineNumber.activeForeground": "#b4bccc",
    "editorGutter.background": "#0f1218",
    "editor.selectionBackground": "#7c8cff40",
    "editorCursor.foreground": "#7c8cff",
    "editorWidget.background": "#151922",
    "editorWidget.border": "#2c3446",
    "scrollbarSlider.background": "#2c344680",
    "diffEditor.insertedTextBackground": "#3fb95026",
    "diffEditor.removedTextBackground": "#f8514926",
  },
});
monaco.editor.defineTheme("studio-light", {
  base: "vs", inherit: true, rules: [],
  colors: {
    "editorLineNumber.foreground": "#9aa1b0",
    "editor.background": "#ffffff",
    "editor.lineHighlightBackground": "#f5f6f9",
    "editor.lineHighlightBorder": "#00000000",
    "editorCursor.foreground": "#4f5bd5",
    "editor.selectionBackground": "#4f5bd533",
  },
});

export { monaco };
export type Monaco = typeof monaco;
