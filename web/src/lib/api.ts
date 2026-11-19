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
  platform: { system: string; python: string };
}
