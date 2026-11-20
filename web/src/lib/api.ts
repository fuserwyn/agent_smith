/* Client for the agent-studio server.
 *
 * The session token arrives once in the URL (?token=...). It is moved to sessionStorage and removed from
 * the address bar, so it is not kept in history or shared by copying the URL. Every API call sends it as
 * a Bearer header; WebSockets (no custom headers in browsers) get it as a query parameter. */

const TOKEN_KEY = "studio-token";

function readToken(): string | null {
  const url = new URL(window.location.href);
  const fromUrl = url.searchParams.get("token");
  if (fromUrl) {
    try { sessionStorage.setItem(TOKEN_KEY, fromUrl); } catch { /* storage blocked: keep it in memory */ }
    url.searchParams.delete("token");
    window.history.replaceState(null, "", url.pathname + url.search + url.hash);
    return fromUrl;
  }
  try { return sessionStorage.getItem(TOKEN_KEY); } catch { return null; }
}

const token = readToken();

export const hasToken = (): boolean => !!token;

export class ApiError extends Error {
  constructor(public status: number, message: string) {
    super(message);
  }
}

export function authHeaders(): Record<string, string> { return token ? { Authorization: `Bearer ${token}` } : {}; }

export async function api<T>(path: string, init: RequestInit = {}): Promise<T> {
  const headers = new Headers(init.headers);
  if (token) headers.set("Authorization", `Bearer ${token}`);
  if (init.method && init.method !== "GET") headers.set("X-Studio", "1");
  if (init.body && !headers.has("Content-Type")) headers.set("Content-Type", "application/json");
  let res: Response;
  try {
    res = await fetch(path, { ...init, headers });
  } catch {
    throw new ApiError(0, "network");
  }
  if (!res.ok) {
    let msg = res.statusText;
    try { msg = ((await res.json()) as { error?: string; detail?: string }).error ?? msg; } catch { /* not JSON */ }
    throw new ApiError(res.status, msg);
  }
  return (await res.json()) as T;
}

export function wsUrl(path: string): string {
  const u = new URL(path, window.location.href);
  u.protocol = u.protocol === "https:" ? "wss:" : "ws:";
  if (token) u.searchParams.set("token", token);
  return u.toString();
}

export interface Health { app: string; version: string; workspace: string }
export interface Session {
  workspace: { name: string; path: string; git: boolean };
  docker: { installed: boolean; running: boolean };
  shell?: "docker" | "local";
  platform: { system: string; python: string };
}

/* ---------- project files ---------- */
export interface Entry { name: string; path: string; type: "dir" | "file"; size: number | null; symlink: boolean; outside: boolean; heavy: boolean; sensitive: boolean }
export interface FileData { path: string; size: number; binary: boolean; too_large: boolean; readonly: boolean; content: string | null; hash: string | null; eol: "lf" | "crlf"; sensitive: boolean }
export interface SearchHit { path: string; line: number; col: number; len: number; text: string; offset: number }
export interface GitFile { path: string; code: string; status: "modified" | "added" | "deleted" | "renamed" | "copied" | "conflict" | "untracked"; staged: boolean; old: string | null }
export interface GitStatus { repo: boolean; branch: string | null; ahead?: number; behind?: number; files: GitFile[] }
export interface GitDiff { path: string; original: string; modified: string; added: boolean; deleted: boolean }

const q = (params: Record<string, string | boolean>) => "?" + new URLSearchParams(Object.entries(params).map(([k, v]) => [k, String(v)])).toString();
export const fs = {
  list: (path = "") => api<{ path: string; entries: Entry[] }>("/api/fs/list" + q({ path })),
  read: (path: string) => api<FileData>("/api/fs/read" + q({ path })),
  write: (path: string, content: string, base_hash: string | null) =>
    api<{ path: string; hash: string; size: number }>("/api/fs/write", { method: "PUT", body: JSON.stringify({ path, content, base_hash }) }),
  create: (path: string, kind: "file" | "dir", content = "") => api<Entry>("/api/fs/create", { method: "POST", body: JSON.stringify({ path, kind, content }) }),
  rename: (from: string, to: string) => api<Entry>("/api/fs/rename", { method: "POST", body: JSON.stringify({ from, to }) }),
  remove: (path: string) => api<{ ok: boolean }>("/api/fs/delete", { method: "POST", body: JSON.stringify({ path }) }),
  search: (qs: string, regex: boolean, cased: boolean) => api<{ results: SearchHit[]; truncated: boolean; files: number }>("/api/fs/search" + q({ q: qs, regex, case: cased })),
  files: () => api<{ files: string[]; truncated: boolean }>("/api/fs/files"),
};
export const gitApi = {
  status: () => api<GitStatus>("/api/git/status"),
  diff: (path: string) => api<GitDiff>("/api/git/diff" + q({ path })),
};

/* ---------- terminals ---------- */
export interface TermInfo { id: string; title: string; mode: "docker" | "local"; alive: boolean; exit_code: number | null; started: number }
export interface TermStatus { mode: "docker" | "local"; available: boolean; reason: string | null; image: string | null; container: string | null; sessions: TermInfo[] }
export const termApi = {
  status: () => api<TermStatus>("/api/terminals"),
  create: (cols: number, rows: number) => api<TermInfo>("/api/terminals", { method: "POST", body: JSON.stringify({ cols, rows }) }),
  kill: (id: string) => api<{ ok: boolean }>(`/api/terminals/${encodeURIComponent(id)}`, { method: "DELETE" }),
};

/* ---------- agent chat ---------- */
export interface ModelChoice { provider: string; model: string; url: string; notes: string; key_env: string | null; key_ok: boolean; paid: boolean }
export interface AgentChange { path: string; status: "added" | "modified" | "deleted" }
export interface AgentRun {
  id: string; mode: "ask" | "agent"; provider: string; model: string; message: string; started: number; ended: number | null;
  status: "running" | "done" | "failed" | "stopped"; answer: string; error: string | null; checkpoint: string | null;
  changes: AgentChange[]; steps: number; input_tokens: number; output_tokens: number;
}
export interface ChatMessage { role: "user" | "assistant"; text: string; run: string; mode?: "ask" | "agent"; status?: AgentRun["status"]; ts: number }
export interface AgentState { messages: ChatMessage[]; runs: Record<string, AgentRun>; current: string | null }
export type TraceEvent = { t: number; type: string; [k: string]: unknown };
export const agentApi = {
  models: () => api<{ models: ModelChoice[]; default: { provider: string; model: string } }>("/api/agent/models"),
  state: () => api<AgentState>("/api/agent/state"),
  start: (message: string, mode: "ask" | "agent", provider?: string, model?: string) =>
    api<AgentRun>("/api/agent/runs", { method: "POST", body: JSON.stringify({ message, mode, provider, model }) }),
  stop: (id: string) => api<{ ok: boolean }>(`/api/agent/runs/${id}/stop`, { method: "POST" }),
  diff: (id: string, path: string) => api<GitDiff>(`/api/agent/runs/${id}/diff` + q({ path })),
  undo: (id: string, paths?: string[]) => api<{ restored: string[]; changes: AgentChange[] }>(`/api/agent/runs/${id}/undo`, { method: "POST", body: JSON.stringify({ paths: paths ?? null }) }),
  trace: async (id: string): Promise<TraceEvent[]> => {
    const res = await fetch(`/api/agent/runs/${id}/trace`, { headers: authHeaders() });
    if (!res.ok) return [];
    return (await res.text()).split("\n").filter(Boolean).map((l) => { try { return JSON.parse(l) as TraceEvent; } catch { return null; } }).filter((x): x is TraceEvent => !!x);
  },
  clear: () => api<{ ok: boolean }>("/api/agent/clear", { method: "POST" }),
};
