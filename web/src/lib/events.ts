/* /ws/events: one WebSocket per tab, reconnects with backoff, fans messages out to subscribers. */
import { useEffect } from "react";
import { wsUrl } from "./api";

export type FsChange = { kind: "added" | "modified" | "deleted"; path: string };
export type StudioEvent = { type: "hello" } | { type: "ping" } | { type: "resync" } | { type: "fs"; changes: FsChange[] } | { type: "fs-watch-stopped" };
type Listener = (e: StudioEvent) => void;

const listeners = new Set<Listener>();
let socket: WebSocket | null = null;
let retry = 0;
let started = false;

function connect() {
  socket = new WebSocket(wsUrl("/ws/events"));
  socket.onopen = () => { retry = 0; };
  socket.onmessage = (m) => {
    let e: StudioEvent;
    try { e = JSON.parse(m.data as string) as StudioEvent; } catch { return; }
    if (e.type === "hello") listeners.forEach((l) => l({ type: "resync" })); // (re)connected: changes may have been missed
    listeners.forEach((l) => l(e));
  };
  socket.onclose = (ev) => {
    if (ev.code === 4401 || ev.code === 4403) return; // no token: the app shows the key screen
    const delay = Math.min(10_000, 500 * 2 ** retry++);
    window.setTimeout(connect, delay);
  };
}

export function useStudioEvents(listener: Listener) {
  useEffect(() => {
    listeners.add(listener);
    if (!started) { started = true; connect(); }
    return () => { listeners.delete(listener); };
  }, [listener]);
}
