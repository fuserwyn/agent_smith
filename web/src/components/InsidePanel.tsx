/* "Inside the agent": the Agent Smith observatory (canvas, step inspector, interpreter, harness) embedded
 * as a bottom tab. It is served by the studio at /observatory (same origin) and receives the trace of a
 * chat turn with postMessage; while the agent works the trace is refreshed live. */
import { useContext, useEffect, useMemo, useRef, useState } from "react";
import { Maximize2, Minimize2, Workflow } from "lucide-react";
import { useAgent } from "../lib/agent";
import { agentApi, type TraceEvent } from "../lib/api";
import { LangContext, useT } from "../lib/i18n";
import { EmptyState } from "./ui";

export function InsidePanel({ visible, request }: { visible: boolean; request: { run: string; n: number } | null }) {
  const t = useT();
  const lang = useContext(LangContext);
  const ag = useAgent();
  const frame = useRef<HTMLIFrameElement>(null);
  const [ready, setReady] = useState(false);
  const [shown, setShown] = useState(false);
  const [run, setRun] = useState<string | null>(null);
  const [max, setMax] = useState(false);
  useEffect(() => {
    if (!max) return;
    const esc = (e: KeyboardEvent) => { if (e.key === "Escape") setMax(false); };
    window.addEventListener("keydown", esc);
    return () => window.removeEventListener("keydown", esc);
  }, [max]);
  const cache = useRef(new Map<string, TraceEvent[]>());
  const lastSent = useRef<{ run: string | null; n: number }>({ run: null, n: -1 });

  const runs = useMemo(() => Object.values(ag.runs).sort((a, b) => b.started - a.started), [ag.runs]);
  useEffect(() => { if (visible) setShown(true); }, [visible]);
  useEffect(() => { if (request) setRun(request.run); }, [request]);
  useEffect(() => { if (!run && runs[0]) setRun(runs[0].id); }, [run, runs]);
  useEffect(() => {
    const on = (e: MessageEvent) => { if (e.origin === location.origin && (e.data as { source?: string })?.source === "agent-smith-observatory") setReady(true); };
    window.addEventListener("message", on);
    return () => window.removeEventListener("message", on);
  }, []);

  const post = (events: TraceEvent[], live: boolean, reset: boolean) => {
    frame.current?.contentWindow?.postMessage({
      source: "agent-smith-studio", events, live, reset, lang,
      theme: document.documentElement.dataset.theme === "light" ? "light" : "dark",
    }, location.origin);
  };

  // send the selected run: its live events while it runs, else its recorded trace (cached)
  const current = run ? ag.runs[run] : undefined;
  const liveCount = run ? ag.events.get(run)?.length ?? 0 : 0;
  useEffect(() => {
    if (!ready || !run || !current) return;
    const live = current.status === "running";
    const isNewRun = lastSent.current.run !== run;
    if (live) {
      const list = ag.events.get(run) ?? [];
      if (!isNewRun && list.length === lastSent.current.n) return;
      const id = window.setTimeout(() => { post([...list], true, isNewRun); lastSent.current = { run, n: list.length }; }, isNewRun ? 0 : 800);
      return () => window.clearTimeout(id);
    }
    if (!isNewRun && lastSent.current.n === -2) return;
    let cancelled = false;
    void (async () => {
      const list = cache.current.get(run) ?? await agentApi.trace(run);
      if (cancelled) return;
      cache.current.set(run, list);
      post(list, false, true);
      lastSent.current = { run, n: -2 };
    })();
    return () => { cancelled = true; };
  }, [ready, run, current?.status, liveCount]);  // eslint-disable-line react-hooks/exhaustive-deps

  // follow the studio's theme and language
  useEffect(() => {
    if (!ready) return;
    const sync = () => post([], false, false);
    sync();
    const obs = new MutationObserver(sync);
    obs.observe(document.documentElement, { attributes: true, attributeFilter: ["data-theme"] });
    return () => obs.disconnect();
  }, [ready, lang]);  // eslint-disable-line react-hooks/exhaustive-deps

  if (!runs.length) return <EmptyState icon={<Workflow size={22} />} title={t("panel.inside")} body={t("in.empty")} />;
  return (
    <div className={`inside${max ? " max" : ""}`}>
      <div className="inside-bar">
        <label className="muted">{t("in.turn")}</label>
        <select className="model-sel wide" value={run ?? ""} onChange={(e) => setRun(e.target.value)} aria-label={t("in.turn")}>
          {runs.map((r) => (
            <option key={r.id} value={r.id}>
              {new Date(r.started * 1000).toLocaleTimeString()} · {r.mode} · {r.message.slice(0, 60)}{r.status === "running" ? ` (${t("in.live")})` : ""}
            </option>
          ))}
        </select>
        {current?.status === "running" && <span className="live-dot">{t("in.live")}</span>}
        <span className="sp" />
        <button type="button" className="icon-btn" title={t(max ? "in.restore" : "in.maximize")} aria-label={t(max ? "in.restore" : "in.maximize")}
                onClick={() => setMax(!max)}>{max ? <Minimize2 size={14} /> : <Maximize2 size={14} />}</button>
      </div>
      {shown && <iframe ref={frame} className="inside-frame" src="/observatory?embed=1" title={t("panel.inside")} />}
    </div>
  );
}
