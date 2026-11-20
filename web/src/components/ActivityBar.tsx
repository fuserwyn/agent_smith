import { Activity, Files, GitBranch, Search, Settings, Sparkles } from "lucide-react";
import type { Key } from "../lib/i18n";
import { useT } from "../lib/i18n";

export type View = "explorer" | "search" | "git" | "runs" | "openrouter" | "settings";

const TOP: { id: View; key: Key; Icon: typeof Files }[] = [
  { id: "explorer", key: "act.explorer", Icon: Files },
  { id: "search", key: "act.search", Icon: Search },
  { id: "git", key: "act.git", Icon: GitBranch },
  { id: "runs", key: "act.runs", Icon: Activity },
  { id: "openrouter", key: "act.openrouter", Icon: Sparkles },
];

/* VS Code-style activity bar: clicking the active view again folds the side panel. */
export function ActivityBar({ view, onSelect }: { view: View | null; onSelect: (v: View) => void }) {
  const t = useT();
  const item = ({ id, key, Icon }: (typeof TOP)[number]) => (
    <button key={id} type="button" className="act" aria-label={t(key)} title={t(key)} aria-pressed={view === id} onClick={() => onSelect(id)}>
      <Icon size={20} strokeWidth={1.7} />
    </button>
  );
  return (
    <nav className="activitybar" aria-label="Views">
      <div className="act-logo" aria-hidden="true">
        <svg viewBox="0 0 32 32" width="22" height="22"><rect width="32" height="32" rx="8" fill="var(--accent)" /><path d="M9 21l5-10 4 7 2-3 3 6" stroke="white" strokeWidth="2.4" fill="none" strokeLinecap="round" strokeLinejoin="round" /></svg>
      </div>
      {TOP.map(item)}
      <span className="act-spacer" />
      {item({ id: "settings", key: "act.settings", Icon: Settings })}
    </nav>
  );
}
