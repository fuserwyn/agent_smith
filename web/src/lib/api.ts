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
/** Code navigation: declarations / uses of a name across the project (studio/navigate.py). */
export interface CodeHit { path: string; line: number; col: number; len: number; text: string; lang: string | null }
export const codeApi = {
  definition: (name: string, path?: string) => api<{ name: string; results: CodeHit[] }>("/api/code/definition" + q(path ? { name, path } : { name })),
  references: (name: string, path?: string) => api<{ name: string; results: CodeHit[] }>("/api/code/references" + q(path ? { name, path } : { name })),
};
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
export interface ModelChoice {
  provider: string; model: string; url: string; notes: string; key_env: string | null; key_ok: boolean; paid: boolean;
  inputs?: string[]; price?: { in: number; out: number } | null;  // from the OpenRouter catalog (what it reads, $ per 1M tokens)
  tier?: "free" | "paid" | "claude";  // free: a provider's free key; paid: your OpenRouter key; claude: Claude Code on your plan
  limits?: string;         // free tier: what the provider caps
  live?: { requests_left?: number; requests_limit?: number; tokens_left?: number; tokens_limit?: number; exhausted_at?: number } | null;
}
/** OpenRouter tab: the whole catalog, the key's balance, the user's picks (chat models, media defaults). */
export type MediaKind = "image" | "video" | "speech" | "transcribe" | "perceive" | "embeddings";
export interface OrModel {
  id: string; name: string; created: number; context: number; in: string[]; out: string[];
  kinds: ("text" | "image" | "video" | "audio" | "embeddings")[]; free: boolean; tools: boolean; description: string;
  price: { in: number | null; out: number | null; image_out: number | null; audio: number | null };
}
export interface OrPicks { chat: string[]; media: Partial<Record<MediaKind, string>> }
export interface OrAccount { key: boolean; usage?: number | null; limit?: number | null; remaining?: number | null; balance?: number; credits?: number; used?: number; free_tier?: boolean; error?: string }
export const openrouterApi = {
  catalog: () => api<{ models: OrModel[]; picks: OrPicks; media_defaults: Record<MediaKind, string[]> }>("/api/openrouter/catalog"),
  account: () => api<OrAccount>("/api/openrouter/account"),
  savePicks: (p: OrPicks) => api<OrPicks>("/api/openrouter/picks", { method: "PUT", body: JSON.stringify(p) }),
};

/** A file given with a chat message (stored in the project under agent_code/uploads/). */
export interface Attachment { id: string; name: string; path: string; mime: string; size: number; kind: "image" | "audio" | "video" | "pdf" | "text" | "file" }
export const uploadsApi = {
  upload: async (file: File | Blob, name: string): Promise<Attachment> => {
    const res = await fetch("/api/uploads", { method: "POST", body: file,
      headers: { ...authHeaders(), "X-Studio": "1", "Content-Type": file.type || "application/octet-stream", "X-Filename": encodeURIComponent(name) } });
    const data = await res.json().catch(() => ({}));
    if (!res.ok) throw new ApiError(res.status, (data as { error?: string }).error ?? res.statusText);
    return data as Attachment;
  },
  url: (id: string) => `/api/uploads/${id}?token=${encodeURIComponent(token ?? "")}`,
};
export const voiceApi = {
  /** tier: the chat's (Free: Groq Whisper on the free key; Paid: the OpenRouter transcribe model) */
  transcribe: async (wav: Blob, lang: string, tier: "free" | "paid" | "claude"): Promise<string> => {
    const res = await fetch("/api/voice", { method: "POST", body: wav, headers: { ...authHeaders(), "X-Studio": "1", "Content-Type": "audio/wav", "X-Language": lang, "X-Tier": tier } });
    const data = await res.json().catch(() => ({}));
    if (!res.ok) throw new ApiError(res.status, (data as { error?: string }).error ?? res.statusText);
    return (data as { text: string }).text;
  },
};
export interface AgentChange { path: string; status: "added" | "modified" | "deleted" }
export interface AgentRun {
  id: string; mode: "ask" | "agent"; provider: string; model: string; message: string; started: number; ended: number | null;
  status: "running" | "done" | "failed" | "stopped"; answer: string; error: string | null; checkpoint: string | null;
  changes: AgentChange[]; steps: number; input_tokens: number; output_tokens: number; attachments?: Attachment[]; paid?: boolean;
  tier?: "free" | "paid" | "claude" | ""; cost_usd?: number | null;
}
export interface ChatMessage { role: "user" | "assistant"; text: string; run: string; mode?: "ask" | "agent"; status?: AgentRun["status"]; ts: number; attachments?: Attachment[] }
export interface AgentState { messages: ChatMessage[]; runs: Record<string, AgentRun>; current: string | null }
export type TraceEvent = { t: number; type: string; [k: string]: unknown };
/** An Agent Smith benchmark run started by the chat agent (tool run_agent_smith). */
export interface ScenarioJob {
  id: string; run: string; action: "mbpp" | "swebench"; params: { task_id?: string; model?: string };
  status: "running" | "done" | "failed" | "cancelled"; verdict: string | null; trace: string | null; dir: string;
  started: number; ended: number | null; steps: { key: string; status: string }[];
}
/** A project folder the agent built and runs in Docker (run_app). */
export interface AppInfo {
  id: string; path: string; name: string; url: string; port: number; container_port: number; container: string;
  run: string | null; status: "starting" | "running" | "failed" | "stopped"; started: number; error: string | null;
}
export const appsApi = {
  list: () => api<{ apps: AppInfo[] }>("/api/apps"),
  logs: (id: string) => api<{ logs: string }>(`/api/apps/${id}/logs`),
  stop: (id: string) => api<AppInfo>(`/api/apps/${id}/stop`, { method: "POST" }),
};
export const scenarioApi = {
  list: () => api<{ available: boolean; reason: string | null; jobs: ScenarioJob[] }>("/api/scenarios"),
  trace: (id: string, since = 0) => api<{ job: ScenarioJob; since: number; events: TraceEvent[] }>(`/api/scenarios/${id}/trace` + q({ since: String(since) })),
};

export const agentApi = {
  models: () => api<{ models: ModelChoice[]; default: { provider: string; model: string }; tiers?: { free: boolean; paid: boolean } }>("/api/agent/models"),
  state: () => api<AgentState>("/api/agent/state"),
  start: (message: string, mode: "ask" | "agent", provider?: string, model?: string, attachments: string[] = []) =>
    api<AgentRun>("/api/agent/runs", { method: "POST", body: JSON.stringify({ message, mode, provider, model, attachments }) }),
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
