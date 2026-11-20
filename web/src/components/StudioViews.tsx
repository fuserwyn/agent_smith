/* Side views of stage 5: agent runs history and settings. */
import { useEffect, useMemo, useState } from "react";
import { Activity, Check, CircleX, KeyRound, LoaderCircle, Monitor, Moon, Square, Sun } from "lucide-react";
import { agentStore, useAgent } from "../lib/agent";
import { termApi, type TermStatus } from "../lib/api";
import { LANGS, type Lang, useT } from "../lib/i18n";
import type { ThemeChoice } from "../lib/prefs";
import { EmptyState, PaneHeader } from "./ui";

export function RunsPanel() {
  const t = useT();
  const ag = useAgent();
  const runs = useMemo(() => Object.values(ag.runs).sort((a, b) => b.started - a.started), [ag.runs]);
  return (
    <div className="sidepanel">
      <PaneHeader title={t("act.runs")} />
      {!runs.length ? <EmptyState icon={<Activity size={22} />} title={t("runs.none")} body={t("runs.noneBody")} /> : (
        <div className="sr-list">
          {runs.map((r) => {
            const Icon = r.status === "running" ? LoaderCircle : r.status === "done" ? Check : r.status === "stopped" ? Square : CircleX;
            return (
              <button key={r.id} type="button" className={`run-row st-${r.status}`} title={r.message}
                      onClick={() => window.dispatchEvent(new CustomEvent("studio:inside", { detail: { run: r.id } }))}>
                <Icon size={13} className={r.status === "running" ? "spin" : ""} />
                <span className="run-main">
                  <span className="nm">{r.message}</span>
                  <span className="dim">{new Date(r.started * 1000).toLocaleString()} · {r.mode} · {t("runs.meta", { n: r.steps, c: r.changes.length })}</span>
                </span>
              </button>
            );
          })}
        </div>
      )}
    </div>
  );
}

const SHORTCUTS: [string, string][] = [
  ["⌘K / ⌘⇧P", "sc.palette"], ["⌘P", "sc.open"], ["⌘S", "sc.save"], ["⌘⇧F", "sc.search"], ["⌘⇧E", "sc.explorer"],
  ["⌘⇧G", "sc.git"], ["⌘B", "sc.side"], ["Ctrl+`", "sc.terminal"], ["⌘L", "sc.chat"], ["Enter / ⇧Enter", "sc.send"],
];
export const kbd = (s: string) => (/Mac|iPhone|iPad/.test(navigator.platform || navigator.userAgent) ? s : s.replace(/⌘/g, "Ctrl+").replace(/⇧/g, "Shift+"));

export function SettingsPanel({ theme, onTheme, lang, onLang }: { theme: ThemeChoice; onTheme: (t: ThemeChoice) => void; lang: Lang; onLang: (l: Lang) => void }) {
  const t = useT();
  const ag = useAgent();
  const [term, setTerm] = useState<TermStatus | null>(null);
  useEffect(() => { void termApi.status().then(setTerm).catch(() => setTerm(null)); if (!ag.loaded) void agentStore.load(); }, []);  // eslint-disable-line react-hooks/exhaustive-deps
  const missing = [...new Set(ag.models.filter((m) => !m.key_ok && m.key_env).map((m) => m.key_env!))];
  return (
    <div className="sidepanel">
      <PaneHeader title={t("act.settings")} />
      <div className="settings">
        <section>
          <h4>{t("set.appearance")}</h4>
          <div className="set-row"><span>{t("set.theme")}</span>
            <div className="seg">{([["system", Monitor], ["dark", Moon], ["light", Sun]] as const).map(([v, I]) => (
              <button key={v} type="button" aria-pressed={theme === v} title={t(`theme.${v}`)} onClick={() => onTheme(v)}><I size={13} /></button>))}</div></div>
          <div className="set-row"><span>{t("set.lang")}</span>
            <div className="seg">{LANGS.map((l) => <button key={l} type="button" aria-pressed={lang === l} onClick={() => onLang(l)}>{l.toUpperCase()}</button>)}</div></div>
        </section>
        <section>
          <h4>{t("set.agent")}</h4>
          <div className="set-row"><span>{t("set.mode")}</span>
            <div className="seg">{(["ask", "agent"] as const).map((m) => <button key={m} type="button" aria-pressed={ag.mode === m} onClick={() => ag.setMode(m)}>{t(m === "ask" ? "ag.ask" : "ag.agent")}</button>)}</div></div>
          <div className="set-models">
            {ag.models.map((m) => (
              <div key={`${m.provider}|${m.model}`} className={`set-model${m.key_ok ? "" : " off"}`}>
                {m.key_ok ? <Check size={12} className="ok" /> : <KeyRound size={12} />}
                <span className="mono nm">{m.model}</span><span className="dim">{m.provider}{m.paid ? " · $" : ""}</span>
              </div>
            ))}
          </div>
          {missing.length > 0 && <p className="set-note">{t("set.keys", { vars: missing.join(", ") })}</p>}
        </section>
        <section>
          <h4>{t("panel.terminal")}</h4>
          {term ? <p className="set-note">{term.mode === "docker" ? t("set.termDocker", { image: term.image ?? "", c: term.container ?? "" }) : t("set.termLocal")}
            {!term.available && term.reason ? ` — ${term.reason}` : ""}</p> : <p className="set-note">…</p>}
        </section>
        <section>
          <h4>{t("set.shortcuts")}</h4>
          <div className="set-keys">{SHORTCUTS.map(([k, label]) => <div key={k}><kbd>{kbd(k)}</kbd><span>{t(label as "sc.save")}</span></div>)}</div>
        </section>
      </div>
    </div>
  );
}
