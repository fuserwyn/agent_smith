/* Open files and editor tabs (VS Code semantics: preview tab, pinned tabs, dirty state).
 *
 * One Monaco model per open file. External changes (agent, terminal, other programs) arrive as file
 * events: a clean file is updated in place (undo history kept), a file with unsaved edits gets a banner
 * instead of being overwritten. Saving sends the hash the editor loaded, so the server refuses to
 * overwrite a file that changed meanwhile ("conflict"). Files that look like secrets are not even
 * downloaded until the user asks to see them. */
import { useSyncExternalStore } from "react";
import type * as Mon from "monaco-editor";
import { agentApi, ApiError, fs, gitApi } from "./api";
import type { FsChange } from "./events";

type Monaco = typeof Mon;
let monacoP: Promise<Monaco> | null = null;
export const loadMonaco = (): Promise<Monaco> => (monacoP ??= import("./monaco").then((m) => m.monaco));

export type TabKind = "file" | "git" | "disk" | "agent";
export interface Tab { id: string; kind: TabKind; path: string; preview: boolean; run?: string }
export interface FileState {
  path: string;
  loading: boolean;
  error: string | null;
  hash: string | null;
  size: number;
  readonly: boolean;
  binary: boolean;
  tooLarge: boolean;
  sensitive: boolean;
  revealed: boolean;
  dirty: boolean;
  disk: "same" | "changed" | "deleted";
  conflict: boolean;
  saving: boolean;
}
export interface DiffState { loading: boolean; error: string | null; label: string }

const tabId = (kind: TabKind, path: string, run?: string) => (run ? `${kind}:${run}:${path}` : `${kind}:${path}`);
const base = (p: string) => p.split("/").pop() || p;

class EditorStore {
  tabs: Tab[] = [];
  active: string | null = null;
  files = new Map<string, FileState>();
  diffs = new Map<string, DiffState>();
  reveal: { path: string; line: number; col: number } | null = null;
  private models = new Map<string, Mon.editor.ITextModel>();
  private originals = new Map<string, Mon.editor.ITextModel>(); // left side of diff tabs, by tab id
  private saved = new Map<string, number>();                     // alternative version id at last load/save
  private version = 0;
  private subs = new Set<() => void>();

  subscribe = (cb: () => void) => { this.subs.add(cb); return () => { this.subs.delete(cb); }; };
  getVersion = () => this.version;
  private emit() { this.version++; this.subs.forEach((s) => s()); }
  private patch(path: string, p: Partial<FileState>) {
    const f = this.files.get(path); if (!f) return;
    this.files.set(path, { ...f, ...p }); this.emit();
  }

  model(path: string) { return this.models.get(path) ?? null; }
  original(id: string) { return this.originals.get(id) ?? null; }
  activeTab(): Tab | null { return this.tabs.find((t) => t.id === this.active) ?? null; }
  dirtyPaths(): string[] { return [...this.files.values()].filter((f) => f.dirty).map((f) => f.path); }
  title(tab: Tab): string {
    const name = base(tab.path);
    const clash = this.tabs.some((t) => t !== tab && base(t.path) === name && t.path !== tab.path);
    const dir = tab.path.split("/").slice(-2, -1)[0];
    return clash && dir ? `${name} · ${dir}` : name;
  }

  /* ---------------- opening ---------------- */
  async open(path: string, opts: { preview?: boolean; line?: number; col?: number; kind?: TabKind; run?: string } = {}) {
    const kind = opts.kind ?? "file";
    const id = tabId(kind, path, opts.run);
    const existing = this.tabs.find((t) => t.id === id);
    if (opts.line) this.reveal = { path, line: opts.line, col: opts.col ?? 1 };
    if (existing) {
      if (!opts.preview) existing.preview = false;
      this.active = id; this.emit();
    } else {
      const tab: Tab = { id, kind, path, preview: !!opts.preview, run: opts.run };
      const idx = opts.preview ? this.tabs.findIndex((t) => t.preview && !this.files.get(t.path)?.dirty) : -1;
      if (idx >= 0) { const old = this.tabs[idx]!; this.tabs = [...this.tabs]; this.tabs[idx] = tab; this.release(old); }
      else this.tabs = [...this.tabs, tab];
      this.active = id; this.emit();
    }
    if (kind === "file") await this.ensureFile(path);
    else await this.loadDiff(path, kind, opts.run);
  }

  async ensureFile(path: string, force = false) {
    const known = this.files.get(path);
    if (known && !force && (known.loading || known.revealed || !known.sensitive)) return;
    if (!known) {
      this.files.set(path, { path, loading: true, error: null, hash: null, size: 0, readonly: false, binary: false, tooLarge: false,
        sensitive: false, revealed: false, dirty: false, disk: "same", conflict: false, saving: false });
      this.emit();
    }
    // secrets (.env, keys): only download once the user asked to see them
    const looksSecret = /(^|\/)(\.env(\..*)?|.*\.(pem|key|p12|pfx)|id_(rsa|ed25519|ecdsa)(\.pub)?|\.netrc|\.npmrc|\.pypirc)$/i.test(path);
    if (looksSecret && !force && !known?.revealed) { this.patch(path, { loading: false, sensitive: true }); return; }
    try {
      const [data, monaco] = await Promise.all([fs.read(path), loadMonaco()]);
      let m = this.models.get(path);
      if (!m && data.content != null) {
        m = monaco.editor.createModel(data.content, undefined, monaco.Uri.from({ scheme: "file", path: "/" + path }));
        if (data.eol === "crlf") m.setEOL(monaco.editor.EndOfLineSequence.CRLF);
        this.models.set(path, m);
        m.onDidChangeContent(() => {
          const dirty = m!.getAlternativeVersionId() !== this.saved.get(path);
          if (this.files.get(path)?.dirty !== dirty) this.patch(path, { dirty });
        });
      } else if (m && data.content != null) {
        m.setValue(data.content);
      }
      if (m) this.saved.set(path, m.getAlternativeVersionId());
      this.patch(path, { loading: false, error: null, hash: data.hash, size: data.size, readonly: data.readonly, binary: data.binary,
        tooLarge: data.too_large, sensitive: data.sensitive, revealed: true, dirty: false, disk: "same", conflict: false });
    } catch (e) {
      this.patch(path, { loading: false, error: e instanceof ApiError ? e.message : String(e) });
    }
  }

  revealSecret(path: string) { return this.ensureFile(path, true); }

  private async loadDiff(path: string, kind: TabKind, run?: string) {
    const id = tabId(kind, path, run);
    this.diffs.set(id, { loading: true, error: null, label: kind === "git" ? "HEAD" : kind === "agent" ? "before" : "disk" });
    this.emit();
    try {
      const monaco = await loadMonaco();
      let left: string;
      if (kind === "git" || kind === "agent") {
        const d = kind === "agent" && run ? await agentApi.diff(run, path) : await gitApi.diff(path);
        left = d.original;
        if (!d.deleted) await this.ensureFile(path);
        if (d.deleted && !this.models.has(path)) {  // nothing on disk: an empty right side
          this.models.set(path, monaco.editor.createModel("", undefined, monaco.Uri.from({ scheme: "deleted", path: "/" + path })));
        }
      } else {
        left = (await fs.read(path)).content ?? "";
        await this.ensureFile(path);
      }
      const uri = monaco.Uri.from({ scheme: kind === "git" ? "git-head" : kind === "agent" ? `before-${run}` : "on-disk", path: "/" + path });
      const prev = this.originals.get(id);
      if (prev) prev.setValue(left);
      else this.originals.set(id, monaco.editor.createModel(left, undefined, uri));
      this.diffs.set(id, { loading: false, error: null, label: kind === "git" ? "HEAD" : kind === "agent" ? "before" : "disk" });
    } catch (e) {
      this.diffs.set(id, { loading: false, error: e instanceof ApiError ? e.message : String(e), label: "" });
    }
    this.emit();
  }

  /* ---------------- tabs ---------------- */
  pin(id: string) { const t = this.tabs.find((x) => x.id === id); if (t && t.preview) { t.preview = false; this.emit(); } }
  activate(id: string) { this.active = id; this.emit(); }

  /** false = the file has unsaved changes and force was not given (the UI asks first). */
  close(id: string, force = false): boolean {
    const tab = this.tabs.find((t) => t.id === id); if (!tab) return true;
    const others = this.tabs.filter((t) => t.path === tab.path && t.id !== id);
    if (!force && tab.kind === "file" && !others.some((t) => t.kind === "file") && this.files.get(tab.path)?.dirty) return false;
    const i = this.tabs.indexOf(tab);
    this.tabs = this.tabs.filter((t) => t !== tab);
    if (this.active === id) this.active = (this.tabs[i] ?? this.tabs[i - 1])?.id ?? null;
    this.release(tab);
    this.emit();
    return true;
  }

  private release(tab: Tab) {
    if (tab.kind !== "file") { this.originals.get(tab.id)?.dispose(); this.originals.delete(tab.id); this.diffs.delete(tab.id); }
    if (!this.tabs.some((t) => t.path === tab.path)) {
      this.models.get(tab.path)?.dispose(); this.models.delete(tab.path); this.files.delete(tab.path); this.saved.delete(tab.path);
    }
  }

  /* ---------------- saving ---------------- */
  async save(path: string, overwrite = false): Promise<boolean> {
    const f = this.files.get(path), m = this.models.get(path);
    if (!f || !m || f.readonly || f.saving) return false;
    const version = m.getAlternativeVersionId();
    this.patch(path, { saving: true });
    try {
      const r = await fs.write(path, m.getValue(), overwrite || f.disk === "deleted" ? null : f.hash);
      this.saved.set(path, version);
      this.patch(path, { saving: false, hash: r.hash, dirty: m.getAlternativeVersionId() !== version, disk: "same", conflict: false });
      return true;
    } catch (e) {
      const conflict = e instanceof ApiError && e.status === 409;
      this.patch(path, { saving: false, conflict, error: conflict ? null : e instanceof ApiError ? e.message : String(e) });
      if (!conflict) throw e;
      return false;
    }
  }

  /** Drop local edits and take the file from disk. */
  reload(path: string) { return this.ensureFile(path, true); }

  compareWithDisk(path: string) { return this.open(path, { kind: "disk" }); }

  /* ---------------- external changes ---------------- */
  async onFs(changes: FsChange[]) {
    for (const c of changes) {
      const f = this.files.get(c.path);
      if (f && f.revealed !== false) {
        if (c.kind === "deleted") { this.patch(c.path, { disk: "deleted" }); continue; }
        try {
          const data = await fs.read(c.path);
          const cur = this.files.get(c.path);
          if (!cur || data.hash === cur.hash) continue;             // our own save, or no real change
          const m = this.models.get(c.path);
          if (!cur.dirty && m && data.content != null) {             // clean: follow the disk, keep undo history
            m.pushEditOperations([], [{ range: m.getFullModelRange(), text: data.content }], () => null);
            this.saved.set(c.path, m.getAlternativeVersionId());
            this.patch(c.path, { hash: data.hash, size: data.size, dirty: false, disk: "same" });
          } else this.patch(c.path, { disk: "changed" });
        } catch { /* gone again or unreadable: the next event tells */ }
      }
      for (const kind of ["git", "disk"] as const) if (this.tabs.some((t) => t.id === tabId(kind, c.path))) void this.loadDiff(c.path, kind);
      for (const t of this.tabs.filter((x) => x.kind === "agent" && x.path === c.path)) void this.loadDiff(c.path, "agent", t.run);
    }
  }

  /** After a rename in the explorer: move clean tabs to the new path. */
  async renamed(from: string, to: string) {
    for (const t of this.tabs.filter((x) => x.path === from || x.path.startsWith(from + "/"))) {
      if (this.files.get(t.path)?.dirty) { this.patch(t.path, { disk: "deleted" }); continue; }
      const next = to + t.path.slice(from.length), wasActive = this.active === t.id;
      this.close(t.id, true);
      await this.open(next, { kind: t.kind });
      if (!wasActive && this.active) this.emit();
    }
  }
}

export const editors = new EditorStore();
export function useEditors(): EditorStore {
  useSyncExternalStore(editors.subscribe, editors.getVersion);
  return editors;
}
