/* Agent chat: conversation, a live timeline of each turn (thought, tools, code, output), the answer,
 * a review card of the files the turn changed (open diff, undo one / all), and the composer. */
import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import { ArrowUp, Boxes, Globe, ScrollText, Bot, Paperclip, Mic, Square, X, FileAudio, FileVideo, FileText, FileCode, File as FileIcon, Check, ChevronRight, CircleStop, FileDiff, FilePen, FilePlus, FileX, LoaderCircle, MessageSquarePlus, Search, SquareTerminal, Undo2, Workflow, BookOpen } from "lucide-react";
import { agentStore, useAgent, type Step, type ToolCall } from "../lib/agent";
import { uploadsApi, voiceApi, type ModelChoice, type AgentRun, type AppInfo, type Attachment, type ScenarioJob } from "../lib/api";
import { Recorder } from "../lib/voice";
import { editors } from "../lib/editors";
import { useStudioEvents } from "../lib/events";
import { useT } from "../lib/i18n";
import { toast } from "../lib/toast";
import { useUi } from "../lib/ui";
import { AppLink } from "./AppsPanel";
import { jobLabel } from "./InsidePanel";
import { Markdown } from "./Markdown";
import { WarnIcon } from "./overlays";
import { fuzzy } from "./QuickOpen";
import { fs } from "../lib/api";
import { EmptyState, IconButton, PaneHeader } from "./ui";

const TOOL_ICON: Record<string, typeof Bot> = {
  read_file: BookOpen, list_files: BookOpen, search_code: Search, search_function_or_class_definition_in_code: Search,
  find_references: Search, edit_file: FilePen, run_command: SquareTerminal, run_tests: Check, get_patch: FileDiff, run_agent_smith: Bot,
  run_app: Boxes, call_app: Globe, app_logs: ScrollText, stop_app: CircleStop,
};
const argPath = (t: ToolCall) => (typeof t.args.filepath === "string" ? t.args.filepath : typeof t.args.directory === "string" ? t.args.directory : null);
const short = (s: string, n: number) => (s.length > n ? s.slice(0, n - 1) + "…" : s);
const fmtS = (s: number) => (s < 60 ? `${s.toFixed(0)}s` : `${Math.floor(s / 60)}m${String(Math.round(s % 60)).padStart(2, "0")}`);

/** "model · $0.1/$0.5 · 📷🎧🎬": price per 1M tokens in/out and what it reads besides text */
export function modelLabel(m: ModelChoice): string {
  const reads = (m.inputs ?? []).map((k) => ({ image: "📷", audio: "🎧", video: "🎬", file: "📄" } as Record<string, string>)[k] ?? "").join("");
  const price = m.price ? (m.price.in === 0 && m.price.out === 0 ? "free" : `$${m.price.in}/$${m.price.out}`) : m.paid ? "$" : "";
  return [m.model, price, reads].filter(Boolean).join(" · ");
}

/** " · $0.012": what the turn cost so far (no limit in the studio, so it is shown), from the catalog price */
function turnCost(run: AgentRun, models: ModelChoice[]): string {
  const p = models.find((m) => m.provider === run.provider && m.model === run.model)?.price;
  if (!p || (!p.in && !p.out)) return "";
  const usd = (run.input_tokens * p.in + run.output_tokens * p.out) / 1e6;
  return ` · $${usd < 0.01 ? usd.toFixed(4) : usd.toFixed(3)}`;
}

function AttIcon({ kind }: { kind: Attachment["kind"] }) {
  const I = kind === "audio" ? FileAudio : kind === "video" ? FileVideo : kind === "pdf" ? FileText : kind === "text" ? FileCode : FileIcon;
  return <I size={13} />;
}

const VENDOR: Record<string, string> = {
  anthropic: "Anthropic", openai: "OpenAI", google: "Google", deepseek: "DeepSeek", qwen: "Qwen", "z-ai": "Z.ai",
  moonshotai: "Moonshot", minimax: "MiniMax", nvidia: "NVIDIA", "x-ai": "xAI", mistralai: "Mistral", meta: "Meta",
};

const CHAT_COMMANDS = [
  { cmd: "/mbpp", args: "[task id] [model]", key: "cmdx.mbpp" },
  { cmd: "/swe", args: "[instance id] [model]", key: "cmdx.swe" },
] as const;

function ToolChip({ t }: { t: ToolCall }) {
  const Icon = TOOL_ICON[t.name] ?? Workflow;
  const path = argPath(t);
  const detail = t.name === "run_agent_smith" ? [t.args.benchmark, t.args.task_id].filter(Boolean).join(" ")
    : t.name === "call_app" ? `${String(t.args.method ?? "GET")} ${String(t.args.route ?? "/")}`
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

/** A project the agent runs in Docker (run_app): where it answers, its state, logs and stop. */
function AppCard({ app }: { app: AppInfo }) {
  const t = useT();
  const ag = useAgent();
  return (
    <div className={`job-card app-card st-${app.status === "running" ? "done" : app.status === "starting" ? "running" : "failed"}`}>
      <div className="job-h">
        {app.status === "starting" ? <LoaderCircle size={13} className="spin" /> : <Boxes size={13} />}
        <span className="nm">{app.name}</span>
        <span className={`job-v ${app.status === "running" ? "ok" : app.status === "starting" ? "" : "bad"}`}>{t(`apps.${app.status}` as "apps.running")}</span>
      </div>
      <div className="job-steps">
        <AppLink app={app} />
        <span className="sp" />
        <button type="button" className="linkbtn job-open" onClick={() => window.dispatchEvent(new CustomEvent("studio:apps", { detail: { id: app.id } }))}>
          <ScrollText size={12} />{t("apps.logs")}</button>
        {app.status === "running" && (
          <button type="button" className="linkbtn job-open" onClick={() => void ag.stopApp(app.id)}><CircleStop size={12} />{t("apps.stop")}</button>
        )}
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
          <span className="tok">{t("ag.tok", { i: (run.input_tokens / 1000).toFixed(1), o: (run.output_tokens / 1000).toFixed(1) })}{turnCost(run, ag.models)}</span>
        </button>
      )}
      {showSteps && (hasEvents ? steps.map((s) => <StepRow key={s.n} s={s} live={live} />) : run.steps > 0 && !live ? <div className="step-note">{t("ag.noTrace")}</div> : null)}
      {ag.jobsOf(run.id).map((j) => <JobCard key={j.id} job={j} />)}
      {ag.appsOf(run.id).map((a) => <AppCard key={a.id} app={a} />)}
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
  const [files, setFiles] = useState<(Attachment | { pending: string })[]>([]);
  const [rec, setRec] = useState<{ r: Recorder; secs: number; level: number } | null>(null);
  const [transcribing, setTranscribing] = useState(false);
  const [drag, setDrag] = useState(false);
  const fileInput = useRef<HTMLInputElement>(null);
  const ready = files.filter((f): f is Attachment => "id" in f);
  const uploading = files.length !== ready.length;
  const addFiles = async (list: FileList | File[]) => {
    for (const f of Array.from(list)) {
      const tag = { pending: f.name || "file" };
      setFiles((x) => [...x, tag]);
      try {
        const a = await uploadsApi.upload(f, f.name || `pasted-${Date.now()}.${(f.type.split("/")[1] || "bin").split(";")[0]}`);
        setFiles((x) => x.map((y) => (y === tag ? a : y)));
      } catch (e) {
        setFiles((x) => x.filter((y) => y !== tag));
        toast(`${f.name}: ${e instanceof Error ? e.message : String(e)}`, "err", 7000);
      }
    }
  };
  // voice: record -> WAV -> transcription on the server -> text in the box (editable before sending)
  useEffect(() => {
    if (!rec) return;
    const id = window.setInterval(() => setRec((r) => r && { ...r, secs: Math.floor((Date.now() - r.r.started) / 1000), level: r.r.level() }), 120);
    return () => window.clearInterval(id);
  }, [!!rec]);  // eslint-disable-line react-hooks/exhaustive-deps
  const toggleVoice = async () => {
    if (rec) {
      const r = rec.r;
      setRec(null); setTranscribing(true);
      try {
        const said = await voiceApi.transcribe(await r.stop(), document.documentElement.lang || "");
        if (said) setText((v) => (v.trim() ? v.trimEnd() + " " : "") + said);
        box.current?.focus();
      } catch (e) { toast(t("voice.failed", { e: e instanceof Error ? e.message : String(e) }), "err", 7000); }
      finally { setTranscribing(false); }
      return;
    }
    try { const r = new Recorder(); await r.start(); setRec({ r, secs: 0, level: 0 }); }
    catch (e) { toast(t("voice.noMic", { e: e instanceof Error ? e.message : String(e) }), "err", 7000); }
  };
  // chat commands: Agent Smith's pipeline at once, without the chat model (see parse_command on the server)
  const cmds = /^\/[a-z]*$/i.test(text.trim()) || /^\/(mbpp|swe)\s+\S*$/i.test(text)
    ? CHAT_COMMANDS.filter((c) => c.cmd.startsWith(text.trim().split(/\s/)[0]!.toLowerCase())) : [];
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
    if ((!msg && !ready.length) || sending || ag.current || uploading) return;
    setSending(true);
    try { await ag.send(msg, ready); setText(""); setFiles([]); }
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

  // grouped by the API that serves them — for OpenRouter (your key) by vendor — cheapest first in each group
  const choices = useMemo(() => {
    const label = (m: ModelChoice) => (m.provider === "openrouter" ? `OpenRouter · ${VENDOR[m.model.split("/")[0]!] ?? m.model.split("/")[0]}` : m.provider);
    const cost = (m: ModelChoice) => (m.price ? m.price.in + m.price.out * 3 : Number.MAX_VALUE);
    const g = new Map<string, ModelChoice[]>();
    for (const m of ag.models) g.set(label(m), [...(g.get(label(m)) ?? []), m]);
    const groups = [...g].map(([k, ms]) => [k, [...ms].sort((a, b) => cost(a) - cost(b) || a.model.localeCompare(b.model))] as const);
    return groups.sort((a, b) => Math.min(...a[1].map(cost)) - Math.min(...b[1].map(cost)) || a[0].localeCompare(b[0]));
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
                <div className="msg-user"><span className={`mode-b ${m.mode ?? "agent"}`}>{t(m.mode === "ask" ? "ag.ask" : "ag.agent")}</span><div className="msg-t">{m.text}</div>
                  {!!m.attachments?.length && <div className="att-row in-msg">{m.attachments.map((f) => (
                    <a key={f.id} className={`att k-${f.kind}`} href={uploadsApi.url(f.id)} target="_blank" rel="noreferrer" title={f.path}>
                      {f.kind === "image" ? <img src={uploadsApi.url(f.id)} alt="" /> : <AttIcon kind={f.kind} />}<span className="nm">{f.name}</span></a>))}</div>}</div>
                {run && <Turn run={run} live={run.status === "running"} />}
              </div>
            );
          }
          return null;
        })}
      </div>
      <div className={`composer${drag ? " drop" : ""}`}
           onDragOver={(e) => { if (e.dataTransfer.types.includes("Files")) { e.preventDefault(); setDrag(true); } }}
           onDragLeave={() => setDrag(false)}
           onDrop={(e) => { if (e.dataTransfer.files.length) { e.preventDefault(); setDrag(false); void addFiles(e.dataTransfer.files); } }}>
        {files.length > 0 && (
          <div className="att-row">
            {files.map((f, k) => "id" in f ? (
              <span key={f.id} className={`att k-${f.kind}`} title={`${f.path} · ${Math.max(1, Math.round(f.size / 1024))} KB`}>
                {f.kind === "image" ? <img src={uploadsApi.url(f.id)} alt="" /> : <AttIcon kind={f.kind} />}
                <span className="nm">{f.name}</span>
                <button type="button" aria-label={t("att.remove")} onClick={() => setFiles((x) => x.filter((_, i) => i !== k))}><X size={11} /></button>
              </span>
            ) : <span key={`p${k}`} className="att pending"><LoaderCircle size={12} className="spin" /><span className="nm">{f.pending}</span></span>)}
          </div>
        )}
        {cmds.length > 0 && (
          <div className="mention" role="listbox" aria-label={t("cmdx.title")}>
            {cmds.map((c) => (
              <div key={c.cmd} role="option" aria-selected={false} className="pal-item cmd-item"
                   onMouseDown={(e) => { e.preventDefault(); setText(c.cmd + " "); box.current?.focus(); }}>
                <span className="mono">{c.cmd} <span className="dim">{c.args}</span></span><span className="dim">{t(c.key)}</span>
              </div>
            ))}
          </div>
        )}
        {mention && (
          <div className="mention" role="listbox">
            {mention.files.map((f, k) => (
              <div key={f} role="option" aria-selected={k === mention.i} className={`pal-item${k === mention.i ? " sel" : ""}`} onMouseDown={(e) => { e.preventDefault(); pick(f); }}>{f}</div>
            ))}
          </div>
        )}
        <input ref={fileInput} type="file" multiple hidden onChange={(e) => { if (e.target.files) void addFiles(e.target.files); e.target.value = ""; }} />
        <textarea ref={box} className="composer-in" rows={1} value={text} placeholder={rec ? t("voice.listening") : transcribing ? t("voice.transcribing") : t(ag.mode === "ask" ? "ag.phAsk" : "ag.phAgent")} aria-label={t("ag.message")}
                  onPaste={(e) => { const imgs = Array.from(e.clipboardData.files); if (imgs.length) { e.preventDefault(); void addFiles(imgs); } }}
                  onChange={(e) => void onChange(e.target.value, e.target.selectionStart)}
                  onKeyDown={(e) => {
                    if (mention) {
                      if (e.key === "ArrowDown") { e.preventDefault(); setMention({ ...mention, i: (mention.i + 1) % mention.files.length }); return; }
                      if (e.key === "ArrowUp") { e.preventDefault(); setMention({ ...mention, i: (mention.i + mention.files.length - 1) % mention.files.length }); return; }
                      if (e.key === "Enter" || e.key === "Tab") { e.preventDefault(); pick(mention.files[mention.i]!); return; }
                      if (e.key === "Escape") { setMention(null); return; }
                    }
                    if (e.key === "Tab" && cmds.length === 1 && !text.includes(" ")) { e.preventDefault(); setText(cmds[0]!.cmd + " "); return; }
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
                {ms.map((m) => <option key={m.model} value={`${m.provider}|${m.model}`} disabled={!m.key_ok} title={m.notes}>{modelLabel(m)}{m.key_ok ? "" : ` (${t("ag.noKey")})`}</option>)}
              </optgroup>
            ))}
          </select>
          <span className="sp" />
          <button type="button" className="cbtn" onClick={() => fileInput.current?.click()} title={t("att.add")} aria-label={t("att.add")}><Paperclip size={15} /></button>
          <button type="button" className={`cbtn${rec ? " rec" : ""}`} onClick={() => void toggleVoice()} disabled={transcribing}
                  title={t(rec ? "voice.stop" : "voice.start")} aria-label={t(rec ? "voice.stop" : "voice.start")} aria-pressed={!!rec}>
            {transcribing ? <LoaderCircle size={15} className="spin" /> : rec ? <><Square size={11} /><span className="rec-t">{Math.floor(rec.secs / 60)}:{String(rec.secs % 60).padStart(2, "0")}</span><i className="lvl" style={{ transform: `scaleY(${0.25 + rec.level})` }} /></> : <Mic size={15} />}
          </button>
          {running
            ? <button type="button" className="send stop" onClick={() => void ag.stop()} title={t("ag.stop")} aria-label={t("ag.stop")}><CircleStop size={16} /></button>
            : <button type="button" className="send" onClick={() => void send()} disabled={(!text.trim() && !ready.length) || sending || uploading} title={t("ag.send")} aria-label={t("ag.send")}>{sending ? <LoaderCircle size={15} className="spin" /> : <ArrowUp size={16} />}</button>}
        </div>
      </div>
    </div>
  );
}
