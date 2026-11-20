/* Agent chat state: conversation, runs, live events of the running turn, model and mode choice. */
import { useSyncExternalStore } from "react";
import { agentApi, scenarioApi, type AgentRun, type ChatMessage, type ModelChoice, type ScenarioJob, type TraceEvent } from "./api";
import type { StudioEvent } from "./events";

export interface ToolCall { name: string; args: Record<string, unknown>; line?: number; result?: string; error?: boolean; ms?: number }
export interface Step {
  n: number; thinking: boolean; running: boolean; thought: string; code: string; tools: ToolCall[];
  output: string; status: string | null; inTok: number; outTok: number; note: string | null;
}

class AgentStore {
  messages: ChatMessage[] = [];
  runs: Record<string, AgentRun> = {};
  jobs: Record<string, ScenarioJob> = {};  // Agent Smith runs started from the chat (run_agent_smith)
  current: string | null = null;
  events = new Map<string, TraceEvent[]>();
  models: ModelChoice[] = [];
  model: { provider: string; model: string } | null = null;
  mode: "ask" | "agent" = (localStorage.getItem("studio-agent-mode") as "ask" | "agent") || "agent";
  loaded = false;
  error: string | null = null;
  private v = 0;
  private subs = new Set<() => void>();
  subscribe = (cb: () => void) => { this.subs.add(cb); return () => { this.subs.delete(cb); }; };
  getV = () => this.v;
  emit() { this.v++; this.subs.forEach((s) => s()); }

  async load() {
    try {
      const [st, m, sc] = await Promise.all([agentApi.state(), agentApi.models(), scenarioApi.list().catch(() => null)]);
      if (sc) this.jobs = Object.fromEntries(sc.jobs.map((j) => [j.id, j]));
      this.messages = st.messages; this.runs = st.runs; this.current = st.current; this.models = m.models;
      let saved: { provider: string; model: string } | null = null;
      try { saved = JSON.parse(localStorage.getItem("studio-agent-model") ?? "null"); } catch { /* none */ }
      const ok = (x: { provider: string; model: string } | null) => !!x && m.models.some((c) => c.provider === x.provider && c.model === x.model);
      this.model = ok(saved) ? saved : m.default;
      if (st.current && !this.events.has(st.current)) this.events.set(st.current, await agentApi.trace(st.current));
      this.loaded = true; this.error = null;
    } catch (e) { this.error = e instanceof Error ? e.message : String(e); }
    this.emit();
  }

  setMode(m: "ask" | "agent") { this.mode = m; try { localStorage.setItem("studio-agent-mode", m); } catch { /* blocked */ } this.emit(); }
  setModel(x: { provider: string; model: string }) { this.model = x; try { localStorage.setItem("studio-agent-model", JSON.stringify(x)); } catch { /* blocked */ } this.emit(); }

  async send(text: string) {
    const run = await agentApi.start(text, this.mode, this.model?.provider, this.model?.model);
    this.runs = { ...this.runs, [run.id]: run };
    this.current = run.id;
    if (!this.events.has(run.id)) this.events.set(run.id, []);
    this.messages = [...this.messages, { role: "user", text, run: run.id, mode: this.mode, ts: run.started }];
    this.emit();
  }

  async stop() { if (this.current) await agentApi.stop(this.current); }

  async clear() { await agentApi.clear(); this.messages = []; this.events.clear(); this.emit(); }
  jobsOf(run: string): ScenarioJob[] { return Object.values(this.jobs).filter((j) => j.run === run).sort((a, b) => a.started - b.started); }

  async undo(run: string, paths?: string[]) {
    const r = await agentApi.undo(run, paths);
    const cur = this.runs[run];
    if (cur) this.runs = { ...this.runs, [run]: { ...cur, changes: r.changes } };
    this.emit();
    return r;
  }

  onEvent(e: StudioEvent) {
    if (e.type === "resync") { void this.load(); return; }
    const ev = e as unknown as { type: string; run?: string | AgentRun; event?: TraceEvent };
    if (ev.type === "agent" && typeof ev.run === "string" && ev.event) {
      const list = this.events.get(ev.run) ?? [];
      list.push(ev.event);
      this.events.set(ev.run, list);
      this.emit();
    } else if (ev.type === "scenario") {
      const { job, started } = e as unknown as { job: ScenarioJob; started: boolean };
      this.jobs = { ...this.jobs, [job.id]: job };
      this.emit();
      // show the inner agent at work on the canvas
      if (started) window.dispatchEvent(new CustomEvent("studio:inside", { detail: { job: job.id } }));
    } else if (ev.type === "agent-run" && ev.run && typeof ev.run === "object") {
      const run = ev.run;
      const was = this.runs[run.id];
      this.runs = { ...this.runs, [run.id]: run };
      if (run.status === "running") this.current = run.id;
      else if (this.current === run.id) this.current = null;
      if (was?.status === "running" && run.status !== "running") void agentApi.state().then((st) => { this.messages = st.messages; this.emit(); });
      this.emit();
    }
  }

  /** Group the raw events of a run into steps for the timeline. */
  steps(run: string): Step[] {
    const out: Step[] = [];
    const get = (n: number) => { let s = out.find((x) => x.n === n); if (!s) { s = { n, thinking: false, running: false, thought: "", code: "", tools: [], output: "", status: null, inTok: 0, outTok: 0, note: null }; out.push(s); } return s; };
    let cur = 0;
    for (const e of this.events.get(run) ?? []) {
      const step = typeof e.step === "number" ? e.step : cur;
      switch (e.type) {
        case "llm_request": cur = step; get(step).thinking = true; break;
        case "llm_response": {
          const s = get(step); s.thinking = false; s.inTok = Number(e.input_tokens ?? 0); s.outTok = Number(e.output_tokens ?? 0);
          const text = String(e.text ?? "");
          s.thought = (text.match(/Thought:\s*([\s\S]*?)(?:```|$)/)?.[1] ?? text.split("```")[0] ?? "").trim();
          break;
        }
        case "extraction": get(step).code = String(e.code ?? ""); break;
        case "sandbox_start": get(cur).running = true; break;
        case "tool_call": get(cur).tools.push({ name: String(e.name ?? "?"), args: (e.args as Record<string, unknown>) ?? {}, line: e.line as number | undefined }); break;
        case "tool_result": {
          const s = get(cur); const tc = [...s.tools].reverse().find((t) => t.name === e.name && t.result === undefined);
          if (tc) { tc.result = String(e.result ?? ""); tc.error = !!e.is_error; tc.ms = Number(e.duration_ms ?? 0); }
          break;
        }
        case "sandbox_end": { const s = get(cur); s.running = false; s.output = String(e.output ?? ""); s.status = String(e.status ?? ""); break; }
        case "llm_fallback": get(cur || 1).note = `→ ${String(e.to ?? "")}`; break;
        case "llm_error": get(cur || 1).note = String(e.error ?? "LLM error"); get(cur || 1).thinking = false; break;
        case "loop_detected": get(step).note = "loop"; break;
      }
    }
    return out.filter((s) => s.n > 0);
  }

  lastEvent(run: string): TraceEvent | undefined { const l = this.events.get(run); return l?.[l.length - 1]; }
}

export const agentStore = new AgentStore();
export function useAgent(): AgentStore {
  useSyncExternalStore(agentStore.subscribe, agentStore.getV);
  return agentStore;
}
