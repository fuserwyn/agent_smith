/* Command palette (Cmd/Ctrl+K or Cmd/Ctrl+Shift+P): every action of the studio, searchable. */
import { useEffect, useMemo, useRef, useState } from "react";
import { useT } from "../lib/i18n";
import { fuzzy } from "./QuickOpen";

export interface Command { id: string; label: string; hint?: string; group: string; run: () => void }

export function CommandPalette({ commands, onClose }: { commands: Command[]; onClose: () => void }) {
  const t = useT();
  const [q, setQ] = useState("");
  const [i, setI] = useState(0);
  const input = useRef<HTMLInputElement>(null);
  const list = useRef<HTMLDivElement>(null);
  useEffect(() => { input.current?.focus(); }, []);
  const results = useMemo(() => {
    if (!q.trim()) return commands;
    return commands.map((c) => ({ c, m: fuzzy(q, `${c.group}: ${c.label}`) })).filter((x) => x.m)
      .sort((a, b) => b.m!.score - a.m!.score).map((x) => x.c);
  }, [q, commands]);
  useEffect(() => { setI(0); }, [q]);
  useEffect(() => { list.current?.children[i]?.scrollIntoView({ block: "nearest" }); }, [i]);
  const run = (c: Command) => { onClose(); window.setTimeout(c.run, 0); };
  return (
    <div className="overlay top" onMouseDown={(e) => { if (e.target === e.currentTarget) onClose(); }}>
      <div className="palette" role="dialog" aria-modal="true" aria-label={t("cmd.title")}>
        <input ref={input} className="field" value={q} placeholder={t("cmd.placeholder")} spellCheck={false}
               onChange={(e) => setQ(e.target.value)}
               onKeyDown={(e) => {
                 if (e.key === "Escape") onClose();
                 else if (e.key === "ArrowDown") { e.preventDefault(); setI((x) => Math.min(results.length - 1, x + 1)); }
                 else if (e.key === "ArrowUp") { e.preventDefault(); setI((x) => Math.max(0, x - 1)); }
                 else if (e.key === "Enter" && results[i]) run(results[i]!);
               }} />
        <div ref={list} className="pal-list" role="listbox">
          {!results.length && <div className="tree-msg">{t("cmd.none")}</div>}
          {results.map((c, k) => (
            <div key={c.id} role="option" aria-selected={k === i} className={`pal-item${k === i ? " sel" : ""}`}
                 onMouseEnter={() => setI(k)} onMouseDown={(e) => { e.preventDefault(); run(c); }}>
              <span className="pal-g">{c.group}</span><span className="nm">{c.label}</span>
              {c.hint && <kbd className="pal-k">{c.hint}</kbd>}
            </div>
          ))}
        </div>
      </div>
    </div>
  );
}
