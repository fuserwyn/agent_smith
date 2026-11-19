import { Container, GitBranch, Monitor, Moon, Sun } from "lucide-react";
import type { Session } from "../lib/api";
import { LANGS, type Lang, useT } from "../lib/i18n";
import type { ThemeChoice } from "../lib/prefs";

export type Conn = "connecting" | "connected" | "offline" | "locked";

export function StatusBar({ conn, session, theme, onTheme, lang, onLang }: {
  conn: Conn; session: Session | null; theme: ThemeChoice; onTheme: (t: ThemeChoice) => void; lang: Lang; onLang: (l: Lang) => void;
}) {
  const t = useT();
  const nextTheme: Record<ThemeChoice, ThemeChoice> = { system: "dark", dark: "light", light: "system" };
  const ThemeIcon = theme === "dark" ? Moon : theme === "light" ? Sun : Monitor;
  const docker = session?.docker;
  return (
    <footer className="statusbar">
      <span className={`sb-item conn ${conn}`} role="status"><i className="dot" />{t(`status.${conn}`)}</span>
      {session && <span className="sb-item mono" title={session.workspace.path}>{session.workspace.name}</span>}
      {session?.workspace.git && <span className="sb-item"><GitBranch size={13} />{t("status.git")}</span>}
      {docker && (
        <span className={`sb-item ${docker.running ? "ok" : "warn"}`}>
          <Container size={13} />{t(docker.running ? "status.docker.ok" : docker.installed ? "status.docker.off" : "status.docker.none")}
        </span>
      )}
      <span className="sb-spacer" />
      <span className="sb-seg" role="group" aria-label="Language">
        {LANGS.map((l) => (
          <button key={l} type="button" aria-pressed={l === lang} onClick={() => onLang(l)}>{l.toUpperCase()}</button>
        ))}
      </span>
      <button type="button" className="sb-btn" onClick={() => onTheme(nextTheme[theme])} title={t(`theme.${theme}`)} aria-label={t(`theme.${theme}`)}>
        <ThemeIcon size={14} />
      </button>
    </footer>
  );
}
