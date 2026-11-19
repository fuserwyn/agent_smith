/* Tiny toast store: transient messages (saved, errors) without a library. */
import { useSyncExternalStore } from "react";

export type Toast = { id: number; kind: "info" | "ok" | "err"; text: string };
let toasts: Toast[] = [];
let seq = 0;
const subs = new Set<() => void>();
const emit = () => subs.forEach((s) => s());

export function toast(text: string, kind: Toast["kind"] = "info", ms = 3500) {
  const t = { id: ++seq, kind, text };
  toasts = [...toasts.slice(-3), t];
  emit();
  window.setTimeout(() => { toasts = toasts.filter((x) => x.id !== t.id); emit(); }, ms);
}
export function useToasts(): Toast[] {
  return useSyncExternalStore((cb) => { subs.add(cb); return () => subs.delete(cb); }, () => toasts);
}
