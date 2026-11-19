/* File tree: lazy folders, keyboard navigation, context menu, git decorations, live refresh. */
import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import { ChevronRight, ChevronsDownUp, Copy, FilePlus, FolderPlus, Link2, Pencil, RotateCw, Trash } from "lucide-react";
import { fs, type Entry, type GitFile } from "../lib/api";
import { editors } from "../lib/editors";
import { useStudioEvents, type StudioEvent } from "../lib/events";
import { useT } from "../lib/i18n";
import { toast } from "../lib/toast";
import { useUi } from "../lib/ui";
import { FileIcon } from "./fileIcon";
import { IconButton, PaneHeader } from "./ui";

const EXP_KEY = "studio-expanded";
const parent = (p: string) => (p.includes("/") ? p.slice(0, p.lastIndexOf("/")) : "");
const GIT_LETTER: Record<GitFile["status"], string> = { modified: "M", added: "A", deleted: "D", renamed: "R", copied: "C", conflict: "!", untracked: "U" };

export function Explorer({ git, workspace }: { git: Map<string, GitFile["status"]>; workspace: string }) {
  const t = useT();
  const ui = useUi();
  const [tree, setTree] = useState<Map<string, Entry[]>>(new Map());
  const [expanded, setExpanded] = useState<Set<string>>(() => {
    try { return new Set(JSON.parse(localStorage.getItem(`${EXP_KEY}:${workspace}`) ?? "[]") as string[]); } catch { return new Set(); }
  });
  const [sel, setSel] = useState<string | null>(null);
  const [error, setError] = useState<string | null>(null);
  const box = useRef<HTMLDivElement>(null);
  const treeRef = useRef(tree); treeRef.current = tree;

  const load = useCallback(async (dir: string) => {
    try {
      const r = await fs.list(dir);
      setTree((m) => new Map(m).set(dir, r.entries));
      if (dir === "") setError(null);
    } catch (e) {
      if (dir === "") setError(e instanceof Error ? e.message : String(e));
      setTree((m) => { const n = new Map(m); n.delete(dir); return n; }); // folder vanished
    }
  }, []);

  useEffect(() => { void load(""); expanded.forEach((d) => void load(d)); }, []);  // eslint-disable-line react-hooks/exhaustive-deps
  useEffect(() => { try { localStorage.setItem(`${EXP_KEY}:${workspace}`, JSON.stringify([...expanded])); } catch { /* blocked */ } }, [expanded, workspace]);

  useStudioEvents(useCallback((e: StudioEvent) => {
    if (e.type === "resync") { [...treeRef.current.keys()].forEach((d) => void load(d)); return; }
    if (e.type !== "fs") return;
    const dirs = new Set<string>();
    for (const c of e.changes) if (c.kind !== "modified") dirs.add(parent(c.path));
    dirs.forEach((d) => { if (treeRef.current.has(d)) void load(d); });
  }, [load]));

  const rows = useMemo(() => {
    const out: { e: Entry; depth: number }[] = [];
    const walk = (dir: string, depth: number) => {
      for (const e of tree.get(dir) ?? []) {
        out.push({ e, depth });
        if (e.type === "dir" && expanded.has(e.path)) walk(e.path, depth + 1);
      }
    };
    walk("", 0);
    return out;
  }, [tree, expanded]);

  const changedDirs = useMemo(() => {
    const s = new Set<string>();
    for (const p of git.keys()) { let d = parent(p); while (d) { s.add(d); d = parent(d); } }
    return s;
  }, [git]);

  const toggle = (dir: string, open?: boolean) => {
    setExpanded((s) => {
      const n = new Set(s);
      const willOpen = open ?? !n.has(dir);
      if (willOpen) { n.add(dir); if (!treeRef.current.has(dir)) void load(dir); } else n.delete(dir);
      return n;
    });
  };

  const activate = (e: Entry, pin: boolean) => {
    setSel(e.path);
    if (e.type === "dir") toggle(e.path);
    else void editors.open(e.path, { preview: !pin });
  };

  /* ---------------- actions ---------------- */
  const targetDir = (e: Entry | null) => (!e ? "" : e.type === "dir" ? e.path : parent(e.path));
  const create = (kind: "file" | "dir", at: Entry | null) => {
    const dir = targetDir(at);
    ui.dialog({
      title: t(kind === "file" ? "fx.newFile" : "fx.newFolder"), confirm: t("fx.create"), cancel: t("cancel"),
      body: dir ? <span className="mono muted">{dir}/</span> : undefined,
      input: { value: "", placeholder: kind === "file" ? "name.py" : "folder" },
      onConfirm: async (name) => {
        const path = dir ? `${dir}/${name}` : name;
        await fs.create(path, kind);
        if (dir) toggle(dir, true);
        await load(dir);
        setSel(path);
        if (kind === "file") void editors.open(path);
      },
    });
  };
  const rename = (e: Entry) => {
    const dot = e.type === "file" ? e.name.lastIndexOf(".") : -1;
    ui.dialog({
      title: t("fx.rename"), confirm: t("fx.rename"), cancel: t("cancel"),
      input: { value: e.name, select: [0, dot > 0 ? dot : e.name.length] },
      onConfirm: async (name) => {
        if (name === e.name) return;
        const to = parent(e.path) ? `${parent(e.path)}/${name}` : name;
        await fs.rename(e.path, to);
        await load(parent(e.path));
        await editors.renamed(e.path, to);
        setSel(to);
      },
    });
  };
  const remove = (e: Entry) => ui.dialog({
    title: t(e.type === "dir" ? "fx.deleteFolderQ" : "fx.deleteFileQ", { name: e.name }),
    body: t("fx.deleteBody"), confirm: t("fx.delete"), cancel: t("cancel"), danger: true,
    onConfirm: async () => { await fs.remove(e.path); await load(parent(e.path)); toast(t("fx.deleted", { name: e.name }), "ok"); },
  });
  const copy = (text: string) => { void navigator.clipboard?.writeText(text).then(() => toast(t("copied"), "ok")); };

  const menuFor = (ev: React.MouseEvent, e: Entry | null) => {
    ev.preventDefault();
    if (e) setSel(e.path);
    ui.menu(ev.clientX, ev.clientY, [
      { label: t("fx.newFile"), icon: <FilePlus size={14} />, onClick: () => create("file", e) },
      { label: t("fx.newFolder"), icon: <FolderPlus size={14} />, onClick: () => create("dir", e) },
      ...(e ? [
        { label: t("fx.rename"), icon: <Pencil size={14} />, onClick: () => rename(e), hint: "F2", sep: true },
        { label: t("fx.copyPath"), icon: <Copy size={14} />, onClick: () => copy(e.path) },
        { label: t("fx.delete"), icon: <Trash size={14} />, onClick: () => remove(e), danger: true, hint: "Del", sep: true },
      ] : []),
    ]);
  };

  /* ---------------- keyboard ---------------- */
  const onKey = (ev: React.KeyboardEvent) => {
    const i = rows.findIndex((r) => r.e.path === sel);
    const cur = rows[i]?.e;
    const go = (j: number) => { const r = rows[Math.max(0, Math.min(rows.length - 1, j))]; if (r) { setSel(r.e.path); box.current?.querySelector<HTMLElement>(`[data-path="${CSS.escape(r.e.path)}"]`)?.scrollIntoView({ block: "nearest" }); } };
    if (ev.key === "ArrowDown") go(i + 1);
    else if (ev.key === "ArrowUp") go(i < 0 ? 0 : i - 1);
    else if (ev.key === "ArrowRight" && cur?.type === "dir") { if (!expanded.has(cur.path)) toggle(cur.path, true); else go(i + 1); }
    else if (ev.key === "ArrowLeft" && cur) { if (cur.type === "dir" && expanded.has(cur.path)) toggle(cur.path, false); else if (parent(cur.path)) setSel(parent(cur.path)); }
    else if (ev.key === "Enter" && cur) activate(cur, true);
    else if (ev.key === "F2" && cur) rename(cur);
    else if ((ev.key === "Delete" || (ev.key === "Backspace" && ev.metaKey)) && cur) remove(cur);
    else return;
    ev.preventDefault();
  };

  return (
    <div className="explorer">
      <PaneHeader title={t("panel.explorer")}>
        <IconButton label={t("fx.newFile")} onClick={() => create("file", rows.find((r) => r.e.path === sel)?.e ?? null)}><FilePlus size={15} /></IconButton>
        <IconButton label={t("fx.newFolder")} onClick={() => create("dir", rows.find((r) => r.e.path === sel)?.e ?? null)}><FolderPlus size={15} /></IconButton>
        <IconButton label={t("fx.refresh")} onClick={() => [...tree.keys()].forEach((d) => void load(d))}><RotateCw size={14} /></IconButton>
        <IconButton label={t("fx.collapse")} onClick={() => setExpanded(new Set())}><ChevronsDownUp size={15} /></IconButton>
      </PaneHeader>
      <div ref={box} className="tree" role="tree" aria-label={t("panel.explorer")} tabIndex={0} onKeyDown={onKey}
           onContextMenu={(ev) => { if (ev.target === ev.currentTarget) menuFor(ev, null); }}>
        {error && <div className="tree-msg err">{error}</div>}
        {!error && !tree.has("") && <div className="tree-msg">{t("loading")}</div>}
        {tree.has("") && rows.length === 0 && <div className="tree-msg">{t("fx.emptyFolder")}</div>}
        {rows.map(({ e, depth }) => {
          const status = git.get(e.path);
          const open = e.type === "dir" && expanded.has(e.path);
          return (
            <div key={e.path} data-path={e.path} role="treeitem" aria-level={depth + 1} aria-selected={sel === e.path}
                 aria-expanded={e.type === "dir" ? open : undefined}
                 className={`row${sel === e.path ? " sel" : ""}${e.heavy ? " heavy" : ""}${status ? ` g-${status}` : ""}`}
                 style={{ paddingLeft: 8 + depth * 12 }}
                 onClick={() => activate(e, false)} onDoubleClick={() => e.type === "file" && activate(e, true)}
                 onContextMenu={(ev) => menuFor(ev, e)} title={e.path}>
              <span className={`chev${open ? " open" : ""}`}>{e.type === "dir" && <ChevronRight size={13} />}</span>
              <FileIcon name={e.name} dir={e.type === "dir"} open={open} sensitive={e.sensitive} />
              <span className="nm">{e.name}</span>
              {e.symlink && <Link2 size={12} className={`lnk${e.outside ? " out" : ""}`} aria-label={e.outside ? t("fx.linkOutside") : t("fx.link")} />}
              {status ? <span className="gs" title={t(`git.${status}`)}>{GIT_LETTER[status]}</span>
                : e.type === "dir" && changedDirs.has(e.path) ? <span className="gdot" /> : null}
            </div>
          );
        })}
      </div>
    </div>
  );
}
