/* The IDE layout: activity bar | side panel | editor over bottom panel | agent.
 * Every split is draggable (react-resizable-panels) and the layout is remembered per viewer. */
import { useRef, useState } from "react";
import { Group, Panel, Separator, useDefaultLayout, type PanelImperativeHandle } from "react-resizable-panels";
import { Bot, FileCode, Files, Plus, ScrollText, SquareTerminal, Workflow } from "lucide-react";
import { ActivityBar, type View } from "./ActivityBar";
import { EmptyState, IconButton, PaneHeader } from "./ui";
import { type Key, useT } from "../lib/i18n";

type BottomTab = "terminal" | "inside" | "output";
const BOTTOM: { id: BottomTab; key: Key; Icon: typeof Files }[] = [
  { id: "terminal", key: "panel.terminal", Icon: SquareTerminal },
  { id: "inside", key: "panel.inside", Icon: Workflow },
  { id: "output", key: "panel.output", Icon: ScrollText },
];

const storage = (() => { try { localStorage.setItem("studio-probe", "1"); return localStorage; } catch { return undefined; } })();

export function Workbench() {
  const t = useT();
  const [view, setView] = useState<View | null>("explorer");
  const [bottom, setBottom] = useState<BottomTab>("terminal");
  const side = useRef<PanelImperativeHandle | null>(null);
  const outer = useDefaultLayout({ id: "studio-outer", storage });
  const center = useDefaultLayout({ id: "studio-center", storage });

  const selectView = (v: View) => {
    const p = side.current;
    if (v === view && p && !p.isCollapsed()) { p.collapse(); setView(null); return; }
    setView(v);
    if (p?.isCollapsed()) p.expand();
  };

  return (
    <div className="workbench">
      <ActivityBar view={view} onSelect={selectView} />
      <Group orientation="horizontal" className="wb-group" defaultLayout={outer.defaultLayout} onLayoutChanged={outer.onLayoutChanged}>
        <Panel id="side" panelRef={side} defaultSize="18" minSize={180} collapsible collapsedSize={0}
               onResize={(s) => { if (s.inPixels === 0) setView(null); }} className="pane side">
          <PaneHeader title={t(view === "search" ? "act.search" : view === "git" ? "act.git" : view === "runs" ? "act.runs" : view === "settings" ? "act.settings" : "panel.explorer")} />
          <EmptyState icon={<Files size={22} />} title={t("panel.explorer")} note={t("explorer.soon")} />
        </Panel>
        <Separator className="sep sep-v" />
        <Panel id="main" minSize={320} className="pane-wrap">
          <Group orientation="vertical" className="wb-group" defaultLayout={center.defaultLayout} onLayoutChanged={center.onLayoutChanged}>
            <Panel id="editor" minSize={120} className="pane editor">
              <div className="tabs-bar" role="tablist" aria-label="Open files" />
              <EmptyState icon={<FileCode size={26} />} title={t("editor.empty.title")} body={t("editor.empty.body")} />
            </Panel>
            <Separator className="sep sep-h" />
            <Panel id="bottom" defaultSize="32" minSize={90} collapsible collapsedSize={34} className="pane bottom">
              <div className="ptabs" role="tablist">
                {BOTTOM.map(({ id, key, Icon }) => (
                  <button key={id} type="button" role="tab" aria-selected={bottom === id} className="ptab" onClick={() => setBottom(id)}>
                    <Icon size={14} />{t(key)}
                  </button>
                ))}
                <span className="ptabs-spacer" />
                {bottom === "terminal" && <IconButton label="+"><Plus size={15} /></IconButton>}
              </div>
              <div className="pbody">
                {bottom === "terminal" && <EmptyState icon={<SquareTerminal size={22} />} title={t("panel.terminal")} note={t("terminal.soon")} />}
                {bottom === "inside" && <EmptyState icon={<Workflow size={22} />} title={t("panel.inside")} note={t("inside.soon")} />}
                {bottom === "output" && <EmptyState icon={<ScrollText size={22} />} title={t("panel.output")} />}
              </div>
            </Panel>
          </Group>
        </Panel>
        <Separator className="sep sep-v" />
        <Panel id="agent" defaultSize="26" minSize={280} className="pane agent">
          <PaneHeader title={t("panel.agent")} />
          <EmptyState icon={<Bot size={26} />} title={t("agent.empty.title")} body={t("agent.empty.body")} note={t("agent.soon")} />
        </Panel>
      </Group>
    </div>
  );
}
