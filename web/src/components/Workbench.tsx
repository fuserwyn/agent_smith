/* The IDE layout: activity bar | side panel | editor over bottom panel | agent.
 * Every split is draggable (react-resizable-panels) and the layout is remembered per viewer. */
import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import { Group, Panel, Separator, useDefaultLayout, type PanelImperativeHandle } from "react-resizable-panels";
import { Boxes, Files, SquareTerminal, Workflow } from "lucide-react";
import { ActivityBar, type View } from "./ActivityBar";
import { AgentPanel } from "./AgentPanel";
import { CommandPalette, type Command } from "./CommandPalette";
import { RunsPanel, SettingsPanel, kbd } from "./StudioViews";
import { EditorArea } from "./EditorArea";
import { Explorer } from "./Explorer";
import { ContextMenu, Dialog, Toaster, type DialogSpec, type MenuItem } from "./overlays";
import { QuickOpen, invalidateFileIndex } from "./QuickOpen";
import { GitPanel, SearchPanel } from "./SidePanels";
import { AppsPanel } from "./AppsPanel";
import { InsidePanel } from "./InsidePanel";
import { TerminalPanel } from "./TerminalPanel";
import { fs, gitApi, type GitFile, type GitStatus } from "../lib/api";
import { agentStore } from "../lib/agent";
import { LANGS, type Lang } from "../lib/i18n";
import type { ThemeChoice } from "../lib/prefs";
import { editors } from "../lib/editors";
import { useStudioEvents, type StudioEvent } from "../lib/events";
import { type Key, useT } from "../lib/i18n";
import { toast } from "../lib/toast";
import { UiContext, type UiApi } from "../lib/ui";

type BottomTab = "terminal" | "inside" | "output";
const BOTTOM: { id: BottomTab; key: Key; Icon: typeof Files }[] = [
  { id: "terminal", key: "panel.terminal", Icon: SquareTerminal },
  { id: "inside", key: "panel.inside", Icon: Workflow },
  { id: "output", key: "panel.output", Icon: Boxes },
];

const storage = (() => { try { localStorage.setItem("studio-probe", "1"); return localStorage; } catch { return undefined; } })();
// Cmd on macOS, Ctrl elsewhere — on a Mac, Ctrl+P / Ctrl+B / Ctrl+S stay with the terminal (shell history, cursor…)
const IS_MAC = /Mac|iPhone|iPad/.test(navigator.platform || navigator.userAgent);
const mod = (e: KeyboardEvent) => (IS_MAC ? e.metaKey && !e.ctrlKey : e.ctrlKey && !e.metaKey);

export function Workbench({ workspace, theme, onTheme, lang, onLang }: {
  workspace: string; theme: ThemeChoice; onTheme: (t: ThemeChoice) => void; lang: Lang; onLang: (l: Lang) => void;
}) {
  const t = useT();
  const [view, setView] = useState<View | null>("explorer");
  const [bottom, setBottom] = useState<BottomTab>("terminal");
  const [dialog, setDialog] = useState<DialogSpec | null>(null);
  const [menu, setMenu] = useState<{ x: number; y: number; items: MenuItem[] } | null>(null);
  const [quickOpen, setQuickOpen] = useState(false);
  const [palette, setPalette] = useState(false);
  const [searchFocus, setSearchFocus] = useState(0);
  const [git, setGit] = useState<GitStatus | null>(null);
  const side = useRef<PanelImperativeHandle | null>(null);
  const bottomPanel = useRef<PanelImperativeHandle | null>(null);
  const [termFocus, setTermFocus] = useState(0);
  const [insideReq, setInsideReq] = useState<{ run: string; n: number } | null>(null);
  const [appsReq, setAppsReq] = useState<{ id: string; n: number } | null>(null);
  useEffect(() => {  // "Logs" on an app card, or the apps item of the status bar
    const on = (e: Event) => {
      const id = (e as CustomEvent<{ id?: string }>).detail?.id ?? "";
      setBottom("output");
      if (bottomPanel.current?.isCollapsed()) bottomPanel.current.expand();
      if (id) setAppsReq((r) => ({ id, n: (r?.n ?? 0) + 1 }));
    };
    window.addEventListener("studio:apps", on);
    return () => window.removeEventListener("studio:apps", on);
  }, []);
  useEffect(() => {  // "Inside the agent" links in the chat
    const on = (e: Event) => {
      const d = (e as CustomEvent<{ run?: string; job?: string }>).detail;
      const run = d?.job ? `job:${d.job}` : d?.run; if (!run) return;  // a chat turn, or an Agent Smith run it started
      setBottom("inside");
      const bp = bottomPanel.current;
      if (bp?.isCollapsed()) bp.expand();
      if (bp && bp.getSize().asPercentage < 55) bp.resize("55%");  // the canvas needs room
      setInsideReq((r) => ({ run, n: (r?.n ?? 0) + 1 }));
    };
    window.addEventListener("studio:inside", on);
    return () => window.removeEventListener("studio:inside", on);
  }, []);
  const outer = useDefaultLayout({ id: "studio-outer", storage });
  const center = useDefaultLayout({ id: "studio-center", storage });

  /* ---------------- git status (refreshed on file events, debounced) ---------------- */
  const gitTimer = useRef<number | undefined>(undefined);
  const refreshGit = useCallback(() => { void gitApi.status().then(setGit).catch(() => setGit({ repo: false, branch: null, files: [] })); }, []);
  useEffect(() => {
    refreshGit();
    const onFocus = () => refreshGit();
    window.addEventListener("focus", onFocus);
    return () => window.removeEventListener("focus", onFocus);
  }, [refreshGit]);
  useStudioEvents(useCallback((e: StudioEvent) => {
    if (e.type === "fs" || e.type === "resync") {
      window.clearTimeout(gitTimer.current);
      gitTimer.current = window.setTimeout(refreshGit, 400);
    }
    if (e.type === "fs") {
      if (e.changes.some((c) => c.kind !== "modified")) invalidateFileIndex();
      void editors.onFs(e.changes);
    }
  }, [refreshGit]));
  const gitMap = useMemo(() => new Map<string, GitFile["status"]>((git?.files ?? []).map((f) => [f.path, f.status])), [git]);

  /* ---------------- UI services ---------------- */
  const closeTab = useCallback((id: string) => {
    if (editors.close(id)) return;
    const tab = editors.tabs.find((x) => x.id === id);
    const name = tab?.path.split("/").pop() ?? "";
    setDialog({
      title: t("ed.unsavedTitle", { name }), body: t("ed.unsavedBody"), confirm: t("ed.save"), cancel: t("cancel"),
      extra: { label: t("ed.dontSave"), onClick: () => { editors.close(id, true); } },
      onConfirm: async () => { if (tab && (await editors.save(tab.path))) editors.close(id); },
    });
  }, [t]);
  const ui: UiApi = useMemo(() => ({ dialog: setDialog, menu: (x, y, items) => setMenu({ x, y, items }), closeTab }), [closeTab]);

  const selectView = useCallback((v: View) => {
    const p = side.current;
    if (v === view && p && !p.isCollapsed()) { p.collapse(); setView(null); return; }
    setView(v);
    if (p?.isCollapsed()) p.expand();
    if (v === "search") setSearchFocus((n) => n + 1);
  }, [view]);

  /* ---------------- keyboard shortcuts + unsaved-changes guard ----------------
     Captured before the editor and the terminal see them (like VS Code's "commands to skip shell"):
     in a terminal Ctrl+P would otherwise be "previous command" and the app shortcut would never fire. */
  const saveActive = useCallback(async () => {
    const p = editors.activeTab()?.path; if (!p) return;
    try { if (await editors.save(p)) toast(t("ed.saved", { name: p.split("/").pop() ?? p }), "ok", 1800); }
    catch (err) { toast(t("ed.saveFailed", { err: err instanceof Error ? err.message : String(err) }), "err", 6000); }
  }, [t]);
  useEffect(() => {
    const onKey = (e: KeyboardEvent) => {
      const k = e.key.toLowerCase();
      let handled = true;
      if (e.ctrlKey && !e.metaKey && e.key === "`") {  // show and focus the terminal
        setBottom("terminal");
        if (bottomPanel.current?.isCollapsed()) bottomPanel.current.expand();
        setTermFocus((n) => n + 1);
      } else if (!mod(e) || e.altKey) handled = false;
      else if (k === "p" && !e.shiftKey) setQuickOpen(true);
      else if ((k === "k" && !e.shiftKey) || (k === "p" && e.shiftKey)) setPalette(true);
      else if (k === "l" && !e.shiftKey) focusChat();
      else if (k === "s" && !e.shiftKey) void saveActive();
      else if (k === "f" && e.shiftKey) { selectView("search"); setSearchFocus((n) => n + 1); }
      else if (k === "e" && e.shiftKey) selectView("explorer");
      else if (k === "g" && e.shiftKey) selectView("git");
      else if (k === "b" && !e.shiftKey) {
        const p = side.current;
        if (p?.isCollapsed()) { p.expand(); setView((v) => v ?? "explorer"); } else if (p) { p.collapse(); setView(null); }
      } else handled = false;
      if (handled) { e.preventDefault(); e.stopPropagation(); }
    };
    const onUnload = (e: BeforeUnloadEvent) => { if (editors.dirtyPaths().length) { e.preventDefault(); e.returnValue = ""; } };
    window.addEventListener("keydown", onKey, true);
    window.addEventListener("beforeunload", onUnload);
    return () => { window.removeEventListener("keydown", onKey, true); window.removeEventListener("beforeunload", onUnload); };
  }, [selectView, saveActive]);

  function focusChat() {
    const p = document.querySelector<HTMLTextAreaElement>(".composer-in");
    p?.focus();
  }
  const showBottom = (tab: BottomTab) => {
    setBottom(tab);
    if (bottomPanel.current?.isCollapsed()) bottomPanel.current.expand();
  };
  const newFile = () => setDialog({
    title: t("fx.newFile"), confirm: t("fx.create"), cancel: t("cancel"), input: { value: "", placeholder: "src/new_module.py" },
    onConfirm: async (path) => { await fs.create(path, "file"); void editors.open(path); },
  });
  const commands: Command[] = useMemo(() => {
    const g = { f: t("cmd.gFile"), v: t("cmd.gView"), te: t("panel.terminal"), a: t("panel.agent"), p: t("cmd.gPrefs") };
    return [
      { id: "open", group: g.f, label: t("cmd.open"), hint: kbd("⌘P"), run: () => setQuickOpen(true) },
      { id: "save", group: g.f, label: t("ed.save"), hint: kbd("⌘S"), run: () => void saveActive() },
      { id: "new", group: g.f, label: t("fx.newFile"), run: newFile },
      { id: "explorer", group: g.v, label: t("act.explorer"), hint: kbd("⌘⇧E"), run: () => selectView("explorer") },
      { id: "search", group: g.v, label: t("act.search"), hint: kbd("⌘⇧F"), run: () => { selectView("search"); setSearchFocus((n) => n + 1); } },
      { id: "git", group: g.v, label: t("act.git"), hint: kbd("⌘⇧G"), run: () => selectView("git") },
      { id: "runs", group: g.v, label: t("act.runs"), run: () => selectView("runs") },
      { id: "settings", group: g.v, label: t("act.settings"), run: () => selectView("settings") },
      { id: "inside", group: g.v, label: t("panel.inside"), run: () => showBottom("inside") },
      { id: "term", group: g.te, label: t("cmd.showTerminal"), hint: "Ctrl+`", run: () => { showBottom("terminal"); setTermFocus((n) => n + 1); } },
      { id: "term-new", group: g.te, label: t("term.new"), run: () => { showBottom("terminal"); window.dispatchEvent(new Event("studio:new-terminal")); } },
      { id: "chat", group: g.a, label: t("cmd.focusChat"), hint: kbd("⌘L"), run: focusChat },
      { id: "ask", group: g.a, label: t("cmd.modeAsk"), run: () => agentStore.setMode("ask") },
      { id: "agent", group: g.a, label: t("cmd.modeAgent"), run: () => agentStore.setMode("agent") },
      { id: "stop", group: g.a, label: t("ag.stop"), run: () => void agentStore.stop() },
      { id: "newchat", group: g.a, label: t("ag.newChat"), run: () => { if (!agentStore.current) void agentStore.clear(); } },
      ...(["system", "dark", "light"] as const).map((th) => ({ id: `theme-${th}`, group: g.p, label: t(`theme.${th}`), run: () => onTheme(th) })),
      ...LANGS.map((l) => ({ id: `lang-${l}`, group: g.p, label: `${t("set.lang")}: ${l.toUpperCase()}`, run: () => onLang(l) })),
    ];
  }, [t, saveActive, selectView, onTheme, onLang]);  // eslint-disable-line react-hooks/exhaustive-deps


  return (
    <UiContext.Provider value={ui}>
      <div className="workbench">
        <ActivityBar view={view} onSelect={selectView} />
        <Group orientation="horizontal" className="wb-group" defaultLayout={outer.defaultLayout} onLayoutChanged={outer.onLayoutChanged}>
          <Panel id="side" panelRef={side} defaultSize="19" minSize={200} collapsible collapsedSize={0}
                 onResize={(s) => { if (s.inPixels === 0) setView(null); }} className="pane side">
            <div className="side-views">
              <div hidden={view !== "explorer" && view !== null}><Explorer git={gitMap} workspace={workspace} /></div>
              {view === "search" && <SearchPanel focusSignal={searchFocus} />}
              {view === "git" && <GitPanel status={git} refresh={refreshGit} />}
              {view === "runs" && <RunsPanel />}
              {view === "settings" && <SettingsPanel theme={theme} onTheme={onTheme} lang={lang} onLang={onLang} />}
            </div>
          </Panel>
          <Separator className="sep sep-v" />
          <Panel id="main" minSize={320} className="pane-wrap">
            <Group orientation="vertical" className="wb-group" defaultLayout={center.defaultLayout} onLayoutChanged={center.onLayoutChanged}>
              <Panel id="editor" minSize={120} className="pane editor">
                <EditorArea />
              </Panel>
              <Separator className="sep sep-h" />
              <Panel id="bottom" panelRef={bottomPanel} defaultSize="30" minSize={90} collapsible collapsedSize={34} className="pane bottom">
                <div className="ptabs" role="tablist">
                  {BOTTOM.map(({ id, key, Icon }) => (
                    <button key={id} type="button" role="tab" aria-selected={bottom === id} className="ptab" onClick={() => setBottom(id)}>
                      <Icon size={14} />{t(key)}
                    </button>
                  ))}
                  <span className="ptabs-spacer" />
                </div>
                <div className="pbody">
                  {/* kept mounted: terminals keep their screen while another tab is shown */}
                  <div className="pbody-term" hidden={bottom !== "terminal"}><TerminalPanel visible={bottom === "terminal"} focusSignal={termFocus} /></div>
                  <div className="pbody-term" hidden={bottom !== "inside"}><InsidePanel visible={bottom === "inside"} request={insideReq} /></div>
                  <div className="pbody-term" hidden={bottom !== "output"}><AppsPanel visible={bottom === "output"} request={appsReq} /></div>
                </div>
              </Panel>
            </Group>
          </Panel>
          <Separator className="sep sep-v" />
          <Panel id="agent" defaultSize="27" minSize={300} className="pane agent">
            <AgentPanel />
          </Panel>
        </Group>
      </div>
      {quickOpen && <QuickOpen onClose={() => setQuickOpen(false)} />}
      {palette && <CommandPalette commands={commands} onClose={() => setPalette(false)} />}
      {dialog && <Dialog spec={dialog} onClose={() => setDialog(null)} />}
      {menu && <ContextMenu x={menu.x} y={menu.y} items={menu.items} onClose={() => setMenu(null)} />}
      <Toaster />
    </UiContext.Provider>
  );
}
