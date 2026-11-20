/* Editor area: tabs, breadcrumbs, Monaco (one editor + one diff editor, models swapped per tab), banners. */
import { useEffect, useRef, useState } from "react";
import type * as Mon from "monaco-editor";
import { Eye, FileCode, GitCompare, LoaderCircle, Lock, X } from "lucide-react";
import { editors, loadMonaco, useEditors, type Tab } from "../lib/editors";
import { useT } from "../lib/i18n";
import { toast } from "../lib/toast";
import { useUi } from "../lib/ui";
import { FileIcon } from "./fileIcon";
import { WarnIcon } from "./overlays";
import { EmptyState } from "./ui";

type Monaco = typeof Mon;
const themeName = () => (document.documentElement.dataset.theme === "light" ? "studio-light" : "studio-dark");

export function EditorArea() {
  const t = useT();
  const ed = useEditors();
  const ui = useUi();
  const tab = ed.activeTab();
  const file = tab ? ed.files.get(tab.path) : undefined;
  const diff = tab && tab.kind !== "file" ? ed.diffs.get(tab.id) : undefined;

  /* ---------------- Monaco instances (created once, lazily) ---------------- */
  const host = useRef<HTMLDivElement>(null);
  const diffHost = useRef<HTMLDivElement>(null);
  const [mon, setMon] = useState<Monaco | null>(null);
  const editor = useRef<Mon.editor.IStandaloneCodeEditor | null>(null);
  const diffEditor = useRef<Mon.editor.IStandaloneDiffEditor | null>(null);
  const views = useRef(new Map<string, Mon.editor.ICodeEditorViewState | null>());
  const shown = useRef<string | null>(null);

  useEffect(() => { void loadMonaco().then(setMon); }, []);
  useEffect(() => {
    if (!mon || !host.current || !diffHost.current || editor.current) return;
    const opts: Mon.editor.IStandaloneEditorConstructionOptions = {
      automaticLayout: true, theme: themeName(), fontFamily: getComputedStyle(document.documentElement).getPropertyValue("--f-mono"),
      fontSize: 13, lineHeight: 20, minimap: { enabled: true, renderCharacters: false, scale: 1 }, scrollBeyondLastLine: false,
      smoothScrolling: true, cursorSmoothCaretAnimation: "on", renderWhitespace: "selection", bracketPairColorization: { enabled: true },
      padding: { top: 8 }, stickyScroll: { enabled: true }, fixedOverflowWidgets: true, tabSize: 4,
    };
    editor.current = mon.editor.create(host.current, opts);
    diffEditor.current = mon.editor.createDiffEditor(diffHost.current, { ...opts, renderSideBySide: true, originalEditable: false, minimap: { enabled: false } } as Mon.editor.IStandaloneDiffEditorConstructionOptions);
    // Cmd/Ctrl+S is handled by the workbench (captured before Monaco), one save path for every focus
    // follow the app theme
    const obs = new MutationObserver(() => mon.editor.setTheme(themeName()));
    obs.observe(document.documentElement, { attributes: true, attributeFilter: ["data-theme"] });
    return () => { obs.disconnect(); editor.current?.dispose(); diffEditor.current?.dispose(); editor.current = null; diffEditor.current = null; };
  }, [mon]);  // eslint-disable-line react-hooks/exhaustive-deps

  /* ---------------- show the active tab ---------------- */
  const model = tab ? ed.model(tab.path) : null;
  const original = tab && tab.kind !== "file" ? ed.original(tab.id) : null;
  useEffect(() => {
    const e = editor.current, d = diffEditor.current;
    if (!e || !d) return;
    if (shown.current && shown.current.startsWith("file:")) views.current.set(shown.current, e.saveViewState());
    if (!tab) { e.setModel(null); shown.current = null; return; }
    if (tab.kind === "file") {
      requestAnimationFrame(() => e.layout());
      if (model && e.getModel() !== model) e.setModel(model);
      if (model && shown.current !== tab.id) { const v = views.current.get(tab.id); if (v) e.restoreViewState(v); }
      e.updateOptions({ readOnly: !!file?.readonly });
      shown.current = tab.id;
      const r = ed.reveal;
      if (r && r.path === tab.path && model) {
        ed.reveal = null;
        e.setPosition({ lineNumber: r.line, column: r.col }); e.revealLineInCenter(r.line); e.focus();
      }
    } else if (original && model) {
      const cur = d.getModel();
      if (cur?.original !== original || cur?.modified !== model) d.setModel({ original, modified: model });
      requestAnimationFrame(() => d.layout());
      shown.current = tab.id;
    }
  });

  const doSave = async (path: string) => {
    try {
      if (await editors.save(path)) toast(t("ed.saved", { name: path.split("/").pop() ?? path }), "ok", 1800);
    } catch (e) { toast(t("ed.saveFailed", { err: e instanceof Error ? e.message : String(e) }), "err", 6000); }
  };

  /* ---------------- banners ---------------- */
  let banner: React.ReactNode = null;
  if (tab?.kind === "file" && file) {
    if (file.conflict) banner = (
      <div className="banner warn" role="alert"><WarnIcon size={15} /><span>{t("ed.conflict")}</span>
        <button type="button" className="btn sm" onClick={() => void editors.compareWithDisk(tab.path)}><GitCompare size={13} />{t("ed.compare")}</button>
        <button type="button" className="btn sm" onClick={() => void editors.save(tab.path, true)}>{t("ed.overwrite")}</button>
        <button type="button" className="btn sm" onClick={() => void editors.reload(tab.path)}>{t("ed.takeDisk")}</button></div>);
    else if (file.disk === "changed") banner = (
      <div className="banner warn" role="alert"><WarnIcon size={15} /><span>{t("ed.changedOnDisk")}</span>
        <button type="button" className="btn sm" onClick={() => void editors.compareWithDisk(tab.path)}><GitCompare size={13} />{t("ed.compare")}</button>
        <button type="button" className="btn sm" onClick={() => void editors.reload(tab.path)}>{t("ed.takeDisk")}</button></div>);
    else if (file.disk === "deleted") banner = (
      <div className="banner err" role="alert"><WarnIcon size={15} /><span>{t("ed.deletedOnDisk")}</span>
        {model && <button type="button" className="btn sm" onClick={() => void doSave(tab.path)}>{t("ed.saveAgain")}</button>}</div>);
    else if (file.tooLarge) banner = <div className="banner info"><Lock size={14} /><span>{t("ed.tooLarge", { mb: (file.size / 1048576).toFixed(1) })}</span></div>;
    else if (file.readonly && !file.binary) banner = <div className="banner info"><Lock size={14} /><span>{t("ed.readonly")}</span></div>;
  }

  /* ---------------- body ---------------- */
  let overlay: React.ReactNode = null;
  if (!tab) overlay = <EmptyState icon={<FileCode size={26} />} title={t("editor.empty.title")} body={t("editor.empty.body")} note={t("ed.shortcuts")} />;
  else if (tab.kind === "file" && file) {
    if (file.loading) overlay = <div className="center muted"><LoaderCircle className="spin" size={18} /></div>;
    else if (file.error && !model) overlay = <EmptyState icon={<WarnIcon size={24} />} title={t("ed.cannotOpen")} body={file.error} />;
    else if (file.sensitive && !file.revealed) overlay = (
      <div className="gate-inline">
        <EmptyState icon={<Lock size={24} />} title={t("ed.secretTitle")} body={t("ed.secretBody")} />
        <button type="button" className="btn" onClick={() => void editors.revealSecret(tab.path)}><Eye size={14} />{t("ed.secretShow")}</button>
      </div>);
    else if (file.binary) overlay = <EmptyState icon={<FileCode size={24} />} title={t("ed.binary")} body={t("ed.binaryBody", { kb: Math.round(file.size / 1024) })} />;
  } else if (diff?.loading) overlay = <div className="center muted"><LoaderCircle className="spin" size={18} /></div>;
  else if (diff?.error) overlay = <EmptyState icon={<WarnIcon size={24} />} title={t("ed.cannotOpen")} body={diff.error} />;

  const isDiff = !!tab && tab.kind !== "file" && !overlay;
  const crumbs = tab ? tab.path.split("/") : [];
  return (
    <div className="editor-area">
      {renderTabs()}
      {tab && (
        <div className="crumbs mono" aria-label={t("ed.path")}>
          {crumbs.map((c, i) => <span key={i}>{i > 0 && <span className="cs">›</span>}{c}</span>)}
          {tab.kind !== "file" && <span className="crumb-tag">{tab.kind === "git" ? t("ed.vsHead") : tab.kind === "agent" ? t("ed.vsAgent") : t("ed.vsDisk")}</span>}
        </div>
      )}
      {banner}
      <div className="ed-stage">
        {/* display:none, not visibility:hidden: Monaco marks inner nodes visible, which would leak through */}
        <div ref={host} className="monaco-host" style={{ display: tab?.kind === "file" && !overlay ? "block" : "none" }} />
        <div ref={diffHost} className="monaco-host" style={{ display: isDiff ? "block" : "none" }} />
        {overlay && <div className="ed-overlay">{overlay}</div>}
        {!mon && tab && !overlay && <div className="ed-overlay center muted"><LoaderCircle className="spin" size={18} /></div>}
      </div>
    </div>
  );

  function renderTabs() {
    return (
      <div className="tabs-bar" role="tablist" aria-label={t("ed.openFiles")}>
        {ed.tabs.map((x: Tab) => {
          const f = ed.files.get(x.path);
          const active = x.id === ed.active;
          return (
            <div key={x.id} role="tab" aria-selected={active} tabIndex={active ? 0 : -1} title={x.path}
                 className={`tab${active ? " active" : ""}${x.preview ? " preview" : ""}${f?.dirty ? " dirty" : ""}`}
                 onClick={() => editors.activate(x.id)} onDoubleClick={() => editors.pin(x.id)}
                 onMouseDown={(e) => { if (e.button === 1) { e.preventDefault(); ui.closeTab(x.id); } }}>
              {x.kind === "file" ? <FileIcon name={x.path} sensitive={f?.sensitive} /> : <GitCompare size={14} className="fi" />}
              <span className="tab-t">{ed.title(x)}{x.kind === "git" ? " (HEAD)" : x.kind === "disk" ? ` (${t("ed.disk")})` : x.kind === "agent" ? ` (${t("ed.agentDiff")})` : ""}</span>
              <button type="button" className="tab-x" aria-label={t("ed.close")} onClick={(e) => { e.stopPropagation(); ui.closeTab(x.id); }}>
                {f?.dirty && x.kind === "file" ? <span className="dirty-dot" /> : <X size={13} />}
              </button>
            </div>
          );
        })}
      </div>
    );
  }
}
