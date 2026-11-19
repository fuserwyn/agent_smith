/* Dialog (prompt / confirm), context menu and toasts: small, accessible, no library. */
import { useEffect, useLayoutEffect, useRef, useState, type ReactNode } from "react";
import { Check, CircleExclamationPoint, CircleX, Info } from "lucide-react";
import { useToasts } from "../lib/toast";

export interface DialogSpec {
  title: string;
  body?: ReactNode;
  input?: { value: string; placeholder?: string; select?: [number, number] };
  confirm: string;
  cancel: string;
  danger?: boolean;
  extra?: { label: string; onClick: () => void | Promise<void> };
  onConfirm: (value: string) => void | Promise<void>;
}

export function Dialog({ spec, onClose }: { spec: DialogSpec; onClose: () => void }) {
  const [value, setValue] = useState(spec.input?.value ?? "");
  const [error, setError] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);
  const input = useRef<HTMLInputElement>(null);
  const confirmBtn = useRef<HTMLButtonElement>(null);
  useLayoutEffect(() => {
    if (input.current) {
      input.current.focus();
      const [a, b] = spec.input?.select ?? [0, value.length];
      input.current.setSelectionRange(a, b);
    } else confirmBtn.current?.focus();
  }, []);  // eslint-disable-line react-hooks/exhaustive-deps
  const run = async (fn: () => void | Promise<void>) => {
    setBusy(true); setError(null);
    try { await fn(); onClose(); } catch (e) { setError(e instanceof Error ? e.message : String(e)); } finally { setBusy(false); }
  };
  const submit = () => { if (!spec.input || value.trim()) void run(() => spec.onConfirm(value.trim())); };
  return (
    <div className="overlay" onMouseDown={(e) => { if (e.target === e.currentTarget) onClose(); }}>
      <div className="dialog" role="dialog" aria-modal="true" aria-label={spec.title}
           onKeyDown={(e) => { if (e.key === "Escape") { e.stopPropagation(); onClose(); } }}>
        <h3>{spec.title}</h3>
        {spec.body && <div className="dialog-b">{spec.body}</div>}
        {spec.input && (
          <input ref={input} className="field" value={value} placeholder={spec.input.placeholder} spellCheck={false}
                 onChange={(e) => setValue(e.target.value)} onKeyDown={(e) => { if (e.key === "Enter") submit(); }} />
        )}
        {error && <div className="dialog-err" role="alert">{error}</div>}
        <div className="dialog-btns">
          {spec.extra && <button type="button" className="btn ghost" onClick={() => void run(spec.extra!.onClick)} disabled={busy}>{spec.extra.label}</button>}
          <span className="sp" />
          <button type="button" className="btn ghost" onClick={onClose}>{spec.cancel}</button>
          <button ref={confirmBtn} type="button" className={`btn ${spec.danger ? "danger" : "primary"}`} onClick={submit} disabled={busy}>{spec.confirm}</button>
        </div>
      </div>
    </div>
  );
}

export interface MenuItem { label: string; icon?: ReactNode; onClick: () => void; danger?: boolean; hint?: string; sep?: boolean }

export function ContextMenu({ x, y, items, onClose }: { x: number; y: number; items: MenuItem[]; onClose: () => void }) {
  const ref = useRef<HTMLDivElement>(null);
  const [pos, setPos] = useState({ x, y });
  useLayoutEffect(() => {  // keep it on screen
    const r = ref.current?.getBoundingClientRect(); if (!r) return;
    setPos({ x: Math.min(x, window.innerWidth - r.width - 8), y: Math.min(y, window.innerHeight - r.height - 8) });
    ref.current?.querySelector<HTMLButtonElement>("button")?.focus();
  }, [x, y]);
  useEffect(() => {
    const close = (e: Event) => { if (!ref.current?.contains(e.target as Node)) onClose(); };
    const key = (e: KeyboardEvent) => { if (e.key === "Escape") onClose(); };
    window.addEventListener("mousedown", close, true); window.addEventListener("keydown", key); window.addEventListener("blur", onClose);
    return () => { window.removeEventListener("mousedown", close, true); window.removeEventListener("keydown", key); window.removeEventListener("blur", onClose); };
  }, [onClose]);
  const move = (e: React.KeyboardEvent) => {
    if (e.key !== "ArrowDown" && e.key !== "ArrowUp") return;
    e.preventDefault();
    const btns = [...(ref.current?.querySelectorAll<HTMLButtonElement>("button") ?? [])];
    const i = btns.indexOf(document.activeElement as HTMLButtonElement);
    btns[(i + (e.key === "ArrowDown" ? 1 : btns.length - 1)) % btns.length]?.focus();
  };
  return (
    <div ref={ref} className="menu" role="menu" style={{ left: pos.x, top: pos.y }} onKeyDown={move}>
      {items.map((it, i) => (
        <div key={i}>
          {it.sep && <div className="menu-sep" role="separator" />}
          <button type="button" role="menuitem" className={it.danger ? "danger" : ""} onClick={() => { onClose(); it.onClick(); }}>
            <span className="mi">{it.icon}</span>{it.label}{it.hint && <kbd>{it.hint}</kbd>}
          </button>
        </div>
      ))}
    </div>
  );
}

export function Toaster() {
  const toasts = useToasts();
  return (
    <div className="toaster" role="status" aria-live="polite">
      {toasts.map((t) => (
        <div key={t.id} className={`toast ${t.kind}`}>
          {t.kind === "ok" ? <Check size={14} /> : t.kind === "err" ? <CircleX size={14} /> : <Info size={14} />}
          <span>{t.text}</span>
        </div>
      ))}
    </div>
  );
}

export const WarnIcon = CircleExclamationPoint;
