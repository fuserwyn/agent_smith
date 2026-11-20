/* The IDE layout: activity bar | side panel | editor over bottom panel | agent.
 * Every split is draggable (react-resizable-panels) and the layout is remembered per viewer. */
import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import { Group, Panel, Separator, useDefaultLayout, type PanelImperativeHandle } from "react-resizable-panels";
import { Activity, Bot, Files, ScrollText, Settings, SquareTerminal, Workflow } from "lucide-react";
import { ActivityBar, type View } from "./ActivityBar";
import { EditorArea } from "./EditorArea";
import { Explorer } from "./Explorer";
import { ContextMenu, Dialog, Toaster, type DialogSpec, type MenuItem } from "./overlays";
import { QuickOpen, invalidateFileIndex } from "./QuickOpen";
import { GitPanel, SearchPanel } from "./SidePanels";
import { TerminalPanel } from "./TerminalPanel";
import { EmptyState, PaneHeader } from "./ui";
import { gitApi, type GitFile, type GitStatus } from "../lib/api";
import { editors } from "../lib/editors";
import { useStudioEvents, type StudioEvent } from "../lib/events";
import { type Key, useT } from "../lib/i18n";
import { UiContext, type UiApi } from "../lib/ui";

type BottomTab = "terminal" | "inside" | "output";
const BOTTOM: { id: BottomTab; key: Key; Icon: typeof Files }[] = [
  { id: "terminal", key: "panel.terminal", Icon: SquareTerminal },
  { id: "inside", key: "panel.inside", Icon: Workflow },
  { id: "output", key: "panel.output", Icon: ScrollText },
];

const storage = (() => { try { localStorage.setItem("studio-probe", "1"); return localStorage; } catch { return undefined; } })();
const mod = (e: KeyboardEvent) => e.metaKey || e.ctrlKey;

export function Workbench({ workspace }: { workspace: string }) {
  const t = useT();
  const [view, setView] = useState<View | null>("explorer");
  const [bottom, setBottom] = useState<BottomTab>("terminal");
  const [dialog, setDialog] = useState<DialogSpec | null>(null);
  const [menu, setMenu] = useState<{ x: number; y: number; items: MenuItem[] } | null>(null);
  const [quickOpen, setQuickOpen] = useState(false);
  const [searchFocus, setSearchFocus] = useState(0);
  const [git, setGit] = useState<GitStatus | null>(null);
  const side = useRef<PanelImperativeHandle | null>(null);
  const bottomPanel = useRef<PanelImperativeHandle | null>(null);
  const [termFocus, setTermFocus] = useState(0);
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

  /* ---------------- keyboard shortcuts + unsaved-changes guard ---------------- */
  useEffect(() => {
    const onKey = (e: KeyboardEvent) => {
      if (e.ctrlKey && e.key === "`") {  // like VS Code: show and focus the terminal
        e.preventDefault();
        setBottom("terminal");
        if (bottomPanel.current?.isCollapsed()) bottomPanel.current.expand();
        setTermFocus((n) => n + 1);
        return;
      }
      if (!mod(e)) return;
      const k = e.key.toLowerCase();
      if (k === "p" && !e.shiftKey) { e.preventDefault(); setQuickOpen(true); }
      else if (k === "s") { const p = editors.activeTab()?.path; if (p) { e.preventDefault(); void editors.save(p); } }
      else if (k === "f" && e.shiftKey) { e.preventDefault(); selectView("search"); setSearchFocus((n) => n + 1); }
      else if (k === "e" && e.shiftKey) { e.preventDefault(); selectView("explorer"); }
      else if (k === "g" && e.shiftKey) { e.preventDefault(); selectView("git"); }
      else if (k === "b" && !e.shiftKey) {
        e.preventDefault();
        const p = side.current; if (!p) return;
        if (p.isCollapsed()) { p.expand(); setView((v) => v ?? "explorer"); } else { p.collapse(); setView(null); }
      }
    };
    const onUnload = (e: BeforeUnloadEvent) => { if (editors.dirtyPaths().length) { e.preventDefault(); e.returnValue = ""; } };
    window.addEventListener("keydown", onKey);
    window.addEventListener("beforeunload", onUnload);
    return () => { window.removeEventListener("keydown", onKey); window.removeEventListener("beforeunload", onUnload); };
  }, [selectView]);

  const placeholder = (Icon: typeof Files, title: Key, note: Key) => (
    <div className="sidepanel"><PaneHeader title={t(title)} /><EmptyState icon={<Icon size={22} />} title={t(title)} note={t(note)} /></div>
  );

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
              {view === "runs" && placeholder(Activity, "act.runs", "runs.soon")}
              {view === "settings" && placeholder(Settings, "act.settings", "settings.soon")}
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
                  {bottom === "inside" && <EmptyState icon={<Workflow size={22} />} title={t("panel.inside")} note={t("inside.soon")} />}
                  {bottom === "output" && <EmptyState icon={<ScrollText size={22} />} title={t("panel.output")} />}
                </div>
              </Panel>
            </Group>
          </Panel>
          <Separator className="sep sep-v" />
          <Panel id="agent" defaultSize="25" minSize={280} className="pane agent">
            <PaneHeader title={t("panel.agent")} />
            <EmptyState icon={<Bot size={26} />} title={t("agent.empty.title")} body={t("agent.empty.body")} note={t("agent.soon")} />
          </Panel>
        </Group>
      </div>
      {quickOpen && <QuickOpen onClose={() => setQuickOpen(false)} />}
      {dialog && <Dialog spec={dialog} onClose={() => setDialog(null)} />}
      {menu && <ContextMenu x={menu.x} y={menu.y} items={menu.items} onClose={() => setMenu(null)} />}
      <Toaster />
    </UiContext.Provider>
  );
}
