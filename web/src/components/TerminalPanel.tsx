/* Terminal panel: several xterm.js sessions, each attached to a server PTY over a WebSocket.
 * Sessions live on the server: a page reload re-attaches and replays the recent output. */
import { useCallback, useEffect, useRef, useState } from "react";
import { Terminal } from "@xterm/xterm";
import { FitAddon } from "@xterm/addon-fit";
import { WebLinksAddon } from "@xterm/addon-web-links";
import "@xterm/xterm/css/xterm.css";
import { Container, Plus, RotateCw, SquareTerminal, X } from "lucide-react";
import { ApiError, termApi, wsUrl, type TermInfo, type TermStatus } from "../lib/api";
import { useT } from "../lib/i18n";
import { toast } from "../lib/toast";
import { WarnIcon } from "./overlays";
import { EmptyState, IconButton } from "./ui";

function themeFromCss() {
  const css = getComputedStyle(document.documentElement);
  const v = (n: string) => css.getPropertyValue(n).trim();
  const light = document.documentElement.dataset.theme === "light";
  return {
    background: v("--bg-2"), foreground: v("--ink"), cursor: v("--accent"), cursorAccent: v("--bg-2"),
    selectionBackground: light ? "#4f5bd533" : "#7c8cff40",
    black: light ? "#1a1e27" : "#2c3446", brightBlack: "#687185",
    red: light ? "#cf222e" : "#f85149", green: light ? "#1a7f37" : "#3fb950", yellow: light ? "#9a6700" : "#d29922",
    blue: light ? "#0969da" : "#58a6ff", magenta: light ? "#8250df" : "#bc8cff", cyan: light ? "#1b7c83" : "#39c5cf",
    white: light ? "#3c4454" : "#b4bccc", brightWhite: light ? "#1a1e27" : "#e6e9f0",
  };
}

interface Live { term: Terminal; fit: FitAddon; socket: WebSocket | null; host: HTMLDivElement; retry: number; exited: boolean; closing: boolean; opened: boolean }

export function TerminalPanel({ visible, focusSignal, onStatus }: { visible: boolean; focusSignal: number; onStatus?: (s: TermStatus) => void }) {
  const t = useT();
  const [status, setStatus] = useState<TermStatus | null>(null);
  const [tabs, setTabs] = useState<TermInfo[]>([]);
  const [active, setActive] = useState<string | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);
  const stage = useRef<HTMLDivElement>(null);
  const live = useRef(new Map<string, Live>());
  const started = useRef(false);

  const attach = useCallback((info: TermInfo) => {
    if (live.current.has(info.id) || !stage.current) return;
    const host = document.createElement("div");
    host.className = "term-host";
    stage.current.appendChild(host);
    const term = new Terminal({
      fontFamily: getComputedStyle(document.documentElement).getPropertyValue("--f-mono"), fontSize: 12.5, lineHeight: 1.25,
      cursorBlink: true, allowProposedApi: false, scrollback: 5000, theme: themeFromCss(), macOptionIsMeta: true,
    });
    const fit = new FitAddon();
    term.loadAddon(fit);
    term.loadAddon(new WebLinksAddon((_e, uri) => window.open(uri, "_blank", "noopener,noreferrer")));
    term.open(host);
    const L: Live = { term, fit, socket: null, host, retry: 0, exited: !info.alive, closing: false, opened: false };
    live.current.set(info.id, L);
    term.onData((data) => { if (L.socket?.readyState === WebSocket.OPEN) L.socket.send(JSON.stringify({ type: "input", data })); });
    term.onResize(({ cols, rows }) => { if (L.socket?.readyState === WebSocket.OPEN) L.socket.send(JSON.stringify({ type: "resize", cols, rows })); });
    const connect = () => {
      const ws = new WebSocket(wsUrl(`/ws/terminal/${encodeURIComponent(info.id)}`));
      ws.binaryType = "arraybuffer";
      L.socket = ws;
      ws.onopen = () => {
        if (L.opened) term.reset();  // the server replays the scrollback on every attach: no duplicates
        L.opened = true; L.retry = 0;
        try { fit.fit(); } catch { /* hidden */ } ws.send(JSON.stringify({ type: "resize", cols: term.cols, rows: term.rows })); };
      ws.onmessage = (m) => {
        if (typeof m.data !== "string") { term.write(new Uint8Array(m.data as ArrayBuffer)); return; }
        try {
          const msg = JSON.parse(m.data) as { type: string; code?: number };
          if (msg.type === "exit") {
            L.exited = true;
            term.write(`\r\n\x1b[2m[${t("term.exited", { code: msg.code ?? "?" })}]\x1b[0m\r\n`);
            setTabs((xs) => xs.map((x) => (x.id === info.id ? { ...x, alive: false, exit_code: msg.code ?? null } : x)));
          }
        } catch { /* ignore */ }
      };
      ws.onclose = (ev) => {
        if (L.closing || L.exited || ev.code === 4404) return;
        const delay = Math.min(8000, 400 * 2 ** L.retry++);
        window.setTimeout(() => { if (!L.closing) connect(); }, delay);
      };
    };
    connect();
  }, [t]);

  const refresh = useCallback(async () => {
    try {
      const s = await termApi.status();
      setStatus(s); onStatus?.(s); setError(null);
      setTabs(s.sessions);
      s.sessions.forEach(attach);
      setActive((a) => (a && s.sessions.some((x) => x.id === a) ? a : s.sessions[s.sessions.length - 1]?.id ?? null));
      return s;
    } catch (e) { setError(e instanceof Error ? e.message : String(e)); return null; }
  }, [attach, onStatus]);

  const create = useCallback(async () => {
    setBusy(true);
    try {
      const cur = active ? live.current.get(active)?.term : undefined;
      const info = await termApi.create(cur?.cols ?? 100, cur?.rows ?? 28);
      setTabs((xs) => [...xs, info]);
      attach(info);
      setActive(info.id);
      setError(null);
    } catch (e) {
      const msg = e instanceof ApiError ? e.message : String(e);
      setError(msg);
      if (!(e instanceof ApiError && e.status === 503)) toast(msg, "err");
    } finally { setBusy(false); }
  }, [active, attach]);

  const kill = useCallback(async (id: string) => {
    const L = live.current.get(id);
    if (L) { L.closing = true; L.socket?.close(); L.term.dispose(); L.host.remove(); live.current.delete(id); }
    setTabs((xs) => xs.filter((x) => x.id !== id));
    setActive((a) => (a === id ? null : a));
    try { await termApi.kill(id); } catch { /* already gone */ }
  }, []);

  // first time the panel is shown: attach existing sessions, or open one
  useEffect(() => {
    if (!visible || started.current) return;
    started.current = true;
    void refresh().then((s) => { if (s && s.available && !s.sessions.length) void create(); });
  }, [visible, refresh, create]);

  useEffect(() => { if (!active && tabs.length) setActive(tabs[tabs.length - 1]!.id); }, [active, tabs]);

  // show only the active terminal, keep it fitted to the panel
  useEffect(() => {
    live.current.forEach((L, id) => { L.host.style.display = id === active ? "block" : "none"; });
    const L = active ? live.current.get(active) : undefined;
    if (!L || !visible) return;
    requestAnimationFrame(() => { try { L.fit.fit(); } catch { /* not laid out */ } L.term.focus(); });
  }, [active, visible, tabs]);
  useEffect(() => {
    const el = stage.current; if (!el) return;
    const ro = new ResizeObserver(() => { const L = active ? live.current.get(active) : undefined; if (L && visible) { try { L.fit.fit(); } catch { /* hidden */ } } });
    ro.observe(el);
    return () => ro.disconnect();
  }, [active, visible]);
  useEffect(() => { if (focusSignal && active) live.current.get(active)?.term.focus(); }, [focusSignal, active]);
  useEffect(() => {  // "New terminal" from the command palette
    const on = () => void create();
    window.addEventListener("studio:new-terminal", on);
    return () => window.removeEventListener("studio:new-terminal", on);
  }, [create]);
  // follow the app theme
  useEffect(() => {
    const obs = new MutationObserver(() => { const th = themeFromCss(); live.current.forEach((L) => { L.term.options.theme = th; }); });
    obs.observe(document.documentElement, { attributes: true, attributeFilter: ["data-theme"] });
    return () => obs.disconnect();
  }, []);
  useEffect(() => () => { live.current.forEach((L) => { L.closing = true; L.socket?.close(); L.term.dispose(); }); live.current.clear(); }, []);

  const unavailable = status && !status.available;
  return (
    <div className="terminal-panel">
      <div className="term-bar">
        {tabs.map((x) => (
          <div key={x.id} className={`term-tab${x.id === active ? " active" : ""}${x.alive ? "" : " dead"}`} role="tab" aria-selected={x.id === active}
               onClick={() => setActive(x.id)} title={x.alive ? x.title : t("term.exited", { code: x.exit_code ?? "?" })}>
            <SquareTerminal size={13} /><span>{x.title}</span>
            <button type="button" className="tab-x" aria-label={t("term.kill")} onClick={(e) => { e.stopPropagation(); void kill(x.id); }}><X size={12} /></button>
          </div>
        ))}
        <IconButton label={t("term.new")} onClick={() => void create()}><Plus size={15} /></IconButton>
        <span className="sp" />
        {status && (
          <span className={`term-mode${status.mode === "local" ? " warn" : ""}`} title={status.mode === "docker" ? `${status.container} · ${status.image}` : t("term.localTip")}>
            {status.mode === "docker" ? <><Container size={12} />{t("term.docker", { image: status.image ?? "" })}</> : <><WarnIcon size={12} />{t("term.local")}</>}
          </span>
        )}
      </div>
      <div className="term-stage" ref={stage}>
        {busy && !tabs.length && <div className="term-msg muted">{t("term.starting")}</div>}
        {unavailable && !tabs.length && (
          <div className="term-msg">
            <EmptyState icon={<WarnIcon size={22} />} title={status.reason ?? t("term.unavailable")} body={t("term.dockerHelp")} />
            <button type="button" className="btn sm" onClick={() => void refresh().then((s) => { if (s?.available) void create(); })}><RotateCw size={13} />{t("retry")}</button>
          </div>
        )}
        {!unavailable && !busy && !tabs.length && status && (
          <div className="term-msg"><EmptyState icon={<SquareTerminal size={22} />} title={t("term.none")} />
            <button type="button" className="btn sm" onClick={() => void create()}><Plus size={13} />{t("term.new")}</button></div>
        )}
        {error && tabs.length > 0 && <div className="term-err">{error}</div>}
      </div>
    </div>
  );
}
