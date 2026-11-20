/* Agent chat: conversation, a live timeline of each turn (thought, tools, code, output), the answer,
 * a review card of the files the turn changed (open diff, undo one / all), and the composer. */
import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import { ArrowUp, Bot, Check, ChevronRight, CircleStop, FileDiff, FilePen, FilePlus, FileX, LoaderCircle, MessageSquarePlus, Search, SquareTerminal, Undo2, Workflow, BookOpen } from "lucide-react";
import { agentStore, useAgent, type Step, type ToolCall } from "../lib/agent";
import type { AgentRun, ScenarioJob } from "../lib/api";
import { editors } from "../lib/editors";
import { useStudioEvents } from "../lib/events";
import { useT } from "../lib/i18n";
import { toast } from "../lib/toast";
import { useUi } from "../lib/ui";
import { jobLabel } from "./InsidePanel";
import { Markdown } from "./Markdown";
import { WarnIcon } from "./overlays";
import { fuzzy } from "./QuickOpen";
import { fs } from "../lib/api";
import { EmptyState, IconButton, PaneHeader } from "./ui";

const TOOL_ICON: Record<string, typeof Bot> = {
  read_file: BookOpen, list_files: BookOpen, search_code: Search, search_function_or_class_definition_in_code: Search,
  find_references: Search, edit_file: FilePen, run_command: SquareTerminal, run_tests: Check, get_patch: FileDiff, run_agent_smith: Bot,
};
const argPath = (t: ToolCall) => (typeof t.args.filepath === "string" ? t.args.filepath : typeof t.args.directory === "string" ? t.args.directory : null);
const short = (s: string, n: number) => (s.length > n ? s.slice(0, n - 1) + "…" : s);
const fmtS = (s: number) => (s < 60 ? `${s.toFixed(0)}s` : `${Math.floor(s / 60)}m${String(Math.round(s % 60)).padStart(2, "0")}`);

function ToolChip({ t }: { t: ToolCall }) {
  const Icon = TOOL_ICON[t.name] ?? Workflow;
  const path = argPath(t);
  const detail = t.name === "run_agent_smith" ? [t.args.benchmark, t.args.task_id].filter(Boolean).join(" ")
    : path ?? (typeof t.args.pattern === "string" ? `“${t.args.pattern}”` : typeof t.args.name === "string" ? t.args.name : typeof t.args.command === "string" ? t.args.command : "");
  return (
    <button type="button" className={`tchip${t.error ? " err" : ""}${t.result === undefined ? " pending" : ""}`}
            title={t.result ? short(t.result, 600) : t.name} onClick={() => { if (path && t.name !== "list_files") void editors.open(path, { preview: true }); }}>
      {t.result === undefined ? <LoaderCircle size={12} className="spin" /> : <Icon size={12} />}
      <span className="tn">{t.name.replace("search_function_or_class_definition_in_code", "find_definition")}</span>
      {detail && <span className="ta">{short(String(detail), 42)}</span>}
    </button>
  );
}

/** An Agent Smith benchmark run started by this turn (run_agent_smith): pipeline progress and verdict. */
function JobCard({ job }: { job: ScenarioJob }) {
  const t = useT();
  const running = job.status === "running";
  const ok = job.verdict ? /^pass/i.test(job.verdict) : job.status === "done";
  return (
    <div className={`job-card st-${job.status}`}>
      <div className="job-h">
        {running ? <LoaderCircle size={13} className="spin" /> : <Workflow size={13} />}
        <span className="nm">{jobLabel(job)}</span>
        {!running && <span className={`job-v ${ok ? "ok" : "bad"}`}>{job.verdict ?? t(`job.${job.status}` as "job.done")}</span>}
      </div>
      <div className="job-steps">
        {job.steps.map((s) => <span key={s.key} className={`job-step s-${s.status}`}>{t(`job.${s.key}` as "job.step_agent")}</span>)}
        <button type="button" className="linkbtn job-open" onClick={() => window.dispatchEvent(new CustomEvent("studio:inside", { detail: { job: job.id } }))}>
          <Workflow size={12} />{t(running ? "job.watch" : "ag.inside")}</button>
      </div>
    </div>
  );
}

function StepRow({ s, live }: { s: Step; live: boolean }) {
  const t = useT();
  const [open, setOpen] = useState(false);
  const busy = live && (s.thinking || s.running);
  return (
    <div className={`step${busy ? " busy" : ""}`}>
      <button type="button" className="step-h" onClick={() => setOpen(!open)} aria-expanded={open}>
        <ChevronRight size={12} className={`chev${open ? " open" : ""}`} />
        <span className="step-n">{s.n}</span>
        <span className="step-t">{s.thinking ? <span className="shimmer">{t("ag.thinking")}</span> : s.thought ? short(s.thought.replace(/\s+/g, " "), 140) : t("ag.step")}</span>
        {s.running && <LoaderCircle size={12} className="spin muted" />}
        {s.status && s.status !== "ok" && s.status !== "final_answer" && <span className="step-bad">{s.status}</span>}
      </button>
      {s.tools.length > 0 && <div className="tchips">{s.tools.map((x, i) => <ToolChip key={i} t={x} />)}</div>}
      {s.note && <div className="step-note">{s.note}</div>}
      {open && (
        <div className="step-d">
          {s.thought && <div className="step-thought">{s.thought}</div>}
          {s.code && <><div className="lbl">{t("ag.code")}</div><pre>{s.code}</pre></>}
          {s.output && <><div className="lbl">{t("ag.output")}</div><pre>{short(s.output, 4000)}</pre></>}
          <div className="lbl">{t("ag.tokens", { i: s.inTok, o: s.outTok })}</div>
        </div>
      )}
    </div>
  );
}

function Changes({ run }: { run: AgentRun }) {
  const t = useT();
  const ui = useUi();
  const [busy, setBusy] = useState(false);
  if (!run.changes.length) return null;
  const undo = (paths?: string[]) => {
    const go = async () => {
      setBusy(true);
      try { const r = await agentStore.undo(run.id, paths); toast(t("ag.undone", { n: r.restored.length }), "ok"); }
      catch (e) { toast(e instanceof Error ? e.message : String(e), "err"); }
      finally { setBusy(false); }
    };
    if (paths) void go();
    else ui.dialog({ title: t("ag.undoAllQ"), body: t("ag.undoAllBody", { n: run.changes.length }), confirm: t("ag.undoAll"), cancel: t("cancel"), danger: true, onConfirm: go });
  };
  const Icon = { added: FilePlus, modified: FilePen, deleted: FileX };
  return (
    <div className="changes">
      <div className="changes-h"><span>{t("ag.changes", { n: run.changes.length })}</span>
        <button type="button" className="btn sm ghost" disabled={busy} onClick={() => undo()}><Undo2 size={13} />{t("ag.undoAll")}</button></div>
      {run.changes.map((c) => {
        const I = Icon[c.status];
        return (
          <div key={c.path} className={`chg g-${c.status === "added" ? "added" : c.status === "deleted" ? "deleted" : "modified"}`}>
            <button type="button" className="chg-open" title={t("ag.review")} onClick={() => void editors.open(c.path, { kind: "agent", run: run.id })}>
              <I size={13} /><span className="nm">{c.path}</span>
            </button>
            <button type="button" className="icon-btn" title={t("ag.undoFile")} aria-label={t("ag.undoFile")} disabled={busy} onClick={() => undo([c.path])}><Undo2 size={13} /></button>
          </div>
        );
      })}
    </div>
  );
}

function Turn({ run, live }: { run: AgentRun; live: boolean }) {
  const t = useT();
  const ag = useAgent();
  const steps = ag.steps(run.id);
  const last = ag.lastEvent(run.id);
  const hasEvents = (ag.events.get(run.id)?.length ?? 0) > 0;
  const [showSteps, setShowSteps] = useState(live);
  useEffect(() => { if (live) setShowSteps(true); }, [live]);
  const secs = ((run.ended ?? Date.now() / 1000) - run.started);
  return (
    <div className="turn">
      {(live || hasEvents || run.steps > 0) && (
        <button type="button" className="turn-sum" onClick={() => setShowSteps(!showSteps)} aria-expanded={showSteps}>
          <ChevronRight size={12} className={`chev${showSteps ? " open" : ""}`} />
          {live ? <LoaderCircle size={13} className="spin" /> : <Workflow size={13} />}
          <span>{live ? (last?.type === "checkpoint" ? t("ag.checkpoint") : t("ag.working", { n: Math.max(run.steps, steps.length) })) : t("ag.worked", { n: run.steps, s: fmtS(secs) })}</span>
          <span className="sp" />
          <span className="tok">{t("ag.tok", { i: (run.input_tokens / 1000).toFixed(1), o: (run.output_tokens / 1000).toFixed(1) })}</span>
        </button>
      )}
      {showSteps && (hasEvents ? steps.map((s) => <StepRow key={s.n} s={s} live={live} />) : run.steps > 0 && !live ? <div className="step-note">{t("ag.noTrace")}</div> : null)}
      {ag.jobsOf(run.id).map((j) => <JobCard key={j.id} job={j} />)}
      {!live && run.answer && <Markdown text={run.answer} />}
      {!live && run.status === "failed" && <div className="turn-err"><WarnIcon size={14} />{run.error ?? t("ag.failed")}</div>}
      {!live && run.status === "stopped" && <div className="turn-note">{t("ag.stopped")}</div>}
      {!live && <Changes run={run} />}
      {!live && (
        <div className="turn-meta">
          <span className="mono">{run.model}</span>
          <button type="button" className="linkbtn" onClick={() => window.dispatchEvent(new CustomEvent("studio:inside", { detail: { run: run.id } }))}>
            <Workflow size={12} />{t("ag.inside")}</button>
        </div>
      )}
    </div>
  );
}

export function AgentPanel() {
  const t = useT();
  const ag = useAgent();
  const [text, setText] = useState("");
  const [sending, setSending] = useState(false);
  const [mention, setMention] = useState<{ q: string; at: number; files: string[]; i: number } | null>(null);
  const list = useRef<HTMLDivElement>(null);
  const box = useRef<HTMLTextAreaElement>(null);
  const fileIndex = useRef<string[] | null>(null);

  useEffect(() => { void agentStore.load(); }, []);
  useStudioEvents(useCallback((e) => agentStore.onEvent(e), []));
  // stick to the bottom while the agent works
  const evCount = ag.current ? ag.events.get(ag.current)?.length ?? 0 : 0;
  useEffect(() => { const el = list.current; if (el && el.scrollHeight - el.scrollTop - el.clientHeight < 160) el.scrollTop = el.scrollHeight; }, [evCount, ag.messages.length]);
  useEffect(() => { const el = box.current; if (el) { el.style.height = "auto"; el.style.height = Math.min(220, el.scrollHeight) + "px"; } }, [text]);

  const send = async () => {
    const msg = text.trim();
    if (!msg || sending || ag.current) return;
    setSending(true);
    try { await ag.send(msg); setText(""); }
    catch (e) { toast(e instanceof Error ? e.message : String(e), "err", 7000); }
    finally { setSending(false); }
  };

  const onChange = async (v: string, caret: number) => {
    setText(v);
    const m = v.slice(0, caret).match(/(^|\s)@([\w./-]*)$/);
    if (!m) { setMention(null); return; }
    if (!fileIndex.current) { try { fileIndex.current = (await fs.files()).files; } catch { fileIndex.current = []; } }
    const q = m[2] ?? "";
    const files = fileIndex.current.map((f) => ({ f, s: fuzzy(q, f) })).filter((x) => x.s).sort((a, b) => b.s!.score - a.s!.score).slice(0, 8).map((x) => x.f);
    setMention(files.length ? { q, at: caret - q.length - 1, files, i: 0 } : null);
  };
  const pick = (f: string) => {
    if (!mention) return;
    const v = text.slice(0, mention.at) + "@" + f + " " + text.slice(mention.at + 1 + mention.q.length);
    setText(v); setMention(null);
    requestAnimationFrame(() => box.current?.focus());
  };

  const choices = useMemo(() => {
    const g = new Map<string, typeof ag.models>();
    for (const m of ag.models) g.set(m.provider, [...(g.get(m.provider) ?? []), m]);
    return [...g];
  }, [ag.models]);
  const running = !!ag.current;
  const runs = ag.runs;

  return (
    <div className="agent-panel">
      <PaneHeader title={t("panel.agent")}>
        <IconButton label={t("ag.newChat")} onClick={() => { if (!running) void ag.clear(); }}><MessageSquarePlus size={15} /></IconButton>
      </PaneHeader>
      <div className="chat" ref={list}>
        {ag.error && <div className="turn-err"><WarnIcon size={14} />{ag.error}</div>}
        {ag.loaded && !ag.messages.length && (
          <EmptyState icon={<Bot size={26} />} title={t("agent.empty.title")} body={t("agent.empty.body")} note={t("ag.tip")} />
        )}
        {ag.messages.map((m, i) => {
          if (m.role === "user") {
            const run = runs[m.run];
            return (
              <div key={i} className="exchange">
                <div className="msg-user"><span className={`mode-b ${m.mode ?? "agent"}`}>{t(m.mode === "ask" ? "ag.ask" : "ag.agent")}</span><div className="msg-t">{m.text}</div></div>
                {run && <Turn run={run} live={run.status === "running"} />}
              </div>
            );
          }
          return null;
        })}
      </div>
      <div className="composer">
        {mention && (
          <div className="mention" role="listbox">
            {mention.files.map((f, k) => (
              <div key={f} role="option" aria-selected={k === mention.i} className={`pal-item${k === mention.i ? " sel" : ""}`} onMouseDown={(e) => { e.preventDefault(); pick(f); }}>{f}</div>
            ))}
          </div>
        )}
        <textarea ref={box} className="composer-in" rows={1} value={text} placeholder={t(ag.mode === "ask" ? "ag.phAsk" : "ag.phAgent")} aria-label={t("ag.message")}
                  onChange={(e) => void onChange(e.target.value, e.target.selectionStart)}
                  onKeyDown={(e) => {
                    if (mention) {
                      if (e.key === "ArrowDown") { e.preventDefault(); setMention({ ...mention, i: (mention.i + 1) % mention.files.length }); return; }
                      if (e.key === "ArrowUp") { e.preventDefault(); setMention({ ...mention, i: (mention.i + mention.files.length - 1) % mention.files.length }); return; }
                      if (e.key === "Enter" || e.key === "Tab") { e.preventDefault(); pick(mention.files[mention.i]!); return; }
                      if (e.key === "Escape") { setMention(null); return; }
                    }
                    if (e.key === "Enter" && !e.shiftKey && !e.nativeEvent.isComposing) { e.preventDefault(); void send(); }
                  }} />
        <div className="composer-bar">
          <div className="seg" role="group" aria-label={t("ag.mode")}>
            {(["ask", "agent"] as const).map((m) => (
              <button key={m} type="button" aria-pressed={ag.mode === m} title={t(m === "ask" ? "ag.askTip" : "ag.agentTip")} onClick={() => ag.setMode(m)}>{t(m === "ask" ? "ag.ask" : "ag.agent")}</button>
            ))}
          </div>
          <select className="model-sel" aria-label={t("ag.model")} value={ag.model ? `${ag.model.provider}|${ag.model.model}` : ""}
                  onChange={(e) => { const [provider, model] = e.target.value.split("|"); ag.setModel({ provider: provider!, model: model! }); }}>
            {choices.map(([prov, ms]) => (
              <optgroup key={prov} label={prov}>
                {ms.map((m) => <option key={m.model} value={`${m.provider}|${m.model}`} disabled={!m.key_ok}>{m.model}{m.key_ok ? "" : ` (${t("ag.noKey")})`}{m.paid ? " · $" : ""}</option>)}
              </optgroup>
            ))}
          </select>
          <span className="sp" />
          {running
            ? <button type="button" className="send stop" onClick={() => void ag.stop()} title={t("ag.stop")} aria-label={t("ag.stop")}><CircleStop size={16} /></button>
            : <button type="button" className="send" onClick={() => void send()} disabled={!text.trim() || sending} title={t("ag.send")} aria-label={t("ag.send")}>{sending ? <LoaderCircle size={15} className="spin" /> : <ArrowUp size={16} />}</button>}
        </div>
      </div>
    </div>
  );
}
