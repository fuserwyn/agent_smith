/* "Inside the agent": the Agent Smith observatory (canvas, step inspector, interpreter, harness) embedded
 * as a bottom tab. It is served by the studio at /observatory (same origin) and receives a trace with
 * postMessage: a chat turn of the studio agent, or an Agent Smith benchmark run (MBPP / SWE-bench) that the
 * chat agent started with run_agent_smith. Both are refreshed live while they run. */
import { useContext, useEffect, useMemo, useRef, useState } from "react";
import { Maximize2, Minimize2, Workflow } from "lucide-react";
import { useAgent } from "../lib/agent";
import { agentApi, scenarioApi, type TraceEvent } from "../lib/api";
import { LangContext, useT } from "../lib/i18n";
import { EmptyState } from "./ui";

const JOB = "job:";
export const jobLabel = (j: { action: string; params: { task_id?: string } }) =>
  `Agent Smith · ${j.action === "swebench" ? "SWE-bench" : "MBPP"}${j.params.task_id ? ` ${j.params.task_id}` : ""}`;

export function InsidePanel({ visible, request }: { visible: boolean; request: { run: string; n: number } | null }) {
  const t = useT();
  const lang = useContext(LangContext);
  const ag = useAgent();
  const frame = useRef<HTMLIFrameElement>(null);
  const [ready, setReady] = useState(false);
  const [shown, setShown] = useState(false);
  const [sel, setSel] = useState<string | null>(null);  // chat run id, or "job:<id>"
  const [max, setMax] = useState(false);
  const [jobEvents, setJobEvents] = useState<Record<string, TraceEvent[]>>({});
  useEffect(() => {
    if (!max) return;
    const esc = (e: KeyboardEvent) => { if (e.key === "Escape") setMax(false); };
    window.addEventListener("keydown", esc);
    return () => window.removeEventListener("keydown", esc);
  }, [max]);
  const cache = useRef(new Map<string, TraceEvent[]>());
  const lastSent = useRef<{ sel: string | null; n: number }>({ sel: null, n: -1 });

  // every chat turn, each followed by the Agent Smith runs it started
  const options = useMemo(() => {
    const out: { key: string; label: string; live: boolean; started: number }[] = [];
    for (const r of Object.values(ag.runs).sort((a, b) => b.started - a.started)) {
      out.push({ key: r.id, live: r.status === "running", started: r.started,
                 label: `${new Date(r.started * 1000).toLocaleTimeString()} · ${r.mode} · ${r.message.slice(0, 60)}` });
      for (const j of ag.jobsOf(r.id)) {
        out.push({ key: JOB + j.id, live: j.status === "running", started: j.started,
                   label: `   ↳ ${jobLabel(j)}${j.verdict ? ` · ${j.verdict}` : ""}` });
      }
    }
    return out;
  }, [ag.runs, ag.jobs]);  // eslint-disable-line react-hooks/exhaustive-deps

  useEffect(() => { if (visible) setShown(true); }, [visible]);
  useEffect(() => { if (request) setSel(request.run); }, [request]);
  useEffect(() => { if (!sel && options[0]) setSel(options[0].key); }, [sel, options]);
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

  const jobId = sel?.startsWith(JOB) ? sel.slice(JOB.length) : null;
  const job = jobId ? ag.jobs[jobId] : undefined;
  const chatRun = sel && !jobId ? ag.runs[sel] : undefined;

  // an Agent Smith run: poll its trace (only the new lines) while it runs, once more when it ends
  useEffect(() => {
    if (!jobId) return;
    let stopped = false;
    let timer = 0;
    const tick = async () => {
      const have = cache.current.get(sel!) ?? [];
      try {
        const r = await scenarioApi.trace(jobId, have.length);
        if (stopped) return;
        if (r.events.length || !cache.current.has(sel!)) {
          const all = [...have, ...r.events];
          cache.current.set(sel!, all);
          setJobEvents((m) => ({ ...m, [jobId]: all }));
        }
        if (r.job.status === "running") timer = window.setTimeout(tick, 1000);
      } catch { if (!stopped) timer = window.setTimeout(tick, 2000); }
    };
    void tick();
    return () => { stopped = true; window.clearTimeout(timer); };
  }, [jobId, job?.status]);  // eslint-disable-line react-hooks/exhaustive-deps

  // send the selection to the observatory: live events while running, else the whole recorded trace
  const live = jobId ? job?.status === "running" : chatRun?.status === "running";
  const liveList = jobId ? jobEvents[jobId] : sel ? ag.events.get(sel) : undefined;
  const liveCount = liveList?.length ?? 0;
  useEffect(() => {
    if (!ready || !sel || (!job && !chatRun)) return;
    const isNew = lastSent.current.sel !== sel;
    if (live || jobId) {
      const list = liveList ?? [];
      if (!isNew && list.length === lastSent.current.n) return;
      // a finished run is sent as a reset (the observatory ignores non-live updates otherwise and would keep
      // only what arrived while it was running)
      const id = window.setTimeout(() => { post([...list], !!live, isNew || !live); lastSent.current = { sel, n: list.length }; }, isNew ? 0 : 600);
      return () => window.clearTimeout(id);
    }
    if (!isNew && lastSent.current.n === -2) return;
    let cancelled = false;
    void (async () => {
      const list = cache.current.get(sel) ?? await agentApi.trace(sel);
      if (cancelled) return;
      cache.current.set(sel, list);
      post(list, false, true);
      lastSent.current = { sel, n: -2 };
    })();
    return () => { cancelled = true; };
  }, [ready, sel, live, liveCount, !!job, !!chatRun]);  // eslint-disable-line react-hooks/exhaustive-deps

  // follow the studio's theme and language
  useEffect(() => {
    if (!ready) return;
    const sync = () => post([], false, false);
    sync();
    const obs = new MutationObserver(sync);
    obs.observe(document.documentElement, { attributes: true, attributeFilter: ["data-theme"] });
    return () => obs.disconnect();
  }, [ready, lang]);  // eslint-disable-line react-hooks/exhaustive-deps

  if (!options.length) return <EmptyState icon={<Workflow size={22} />} title={t("panel.inside")} body={t("in.empty")} />;
  return (
    <div className={`inside${max ? " max" : ""}`}>
      <div className="inside-bar">
        <label className="muted">{t("in.turn")}</label>
        <select className="model-sel wide" value={sel ?? ""} onChange={(e) => setSel(e.target.value)} aria-label={t("in.turn")}>
          {options.map((o) => <option key={o.key} value={o.key}>{o.label}{o.live ? ` (${t("in.live")})` : ""}</option>)}
        </select>
        {live && <span className="live-dot">{t("in.live")}</span>}
        {job && job.status === "running" && !liveCount && <span className="muted">{t("in.preparing")}</span>}
        <span className="sp" />
        <button type="button" className="icon-btn" title={t(max ? "in.restore" : "in.maximize")} aria-label={t(max ? "in.restore" : "in.maximize")}
                onClick={() => setMax(!max)}>{max ? <Minimize2 size={14} /> : <Maximize2 size={14} />}</button>
      </div>
      {shown && <iframe ref={frame} className="inside-frame" src="/observatory?embed=1" title={t("panel.inside")} />}
    </div>
  );
}
