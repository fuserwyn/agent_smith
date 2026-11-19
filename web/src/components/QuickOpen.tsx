/* Quick open (Cmd/Ctrl+P): fuzzy file finder over the project index. */
import { useEffect, useMemo, useRef, useState } from "react";
import { fs } from "../lib/api";
import { editors } from "../lib/editors";
import { useT } from "../lib/i18n";
import { FileIcon } from "./fileIcon";

/** Subsequence match with bonuses for consecutive letters, word starts and the file name. */
export function fuzzy(query: string, path: string): { score: number; hits: number[] } | null {
  const q = query.toLowerCase().replace(/\s+/g, ""), p = path.toLowerCase();
  if (!q) return { score: 0, hits: [] };
  const nameStart = p.lastIndexOf("/") + 1;
  let score = 0, prev = -2, j = 0;
  const hits: number[] = [];
  for (let i = 0; i < p.length && j < q.length; i++) {
    if (p[i] !== q[j]) continue;
    hits.push(i);
    score += 1 + (prev === i - 1 ? 3 : 0) + (i === 0 || "/_-. ".includes(p[i - 1]!) ? 2 : 0) + (i >= nameStart ? 2 : 0);
    prev = i; j++;
  }
  if (j < q.length) return null;
  return { score: score - p.length * 0.01, hits };
}

let cache: string[] | null = null;
export const invalidateFileIndex = () => { cache = null; };

export function QuickOpen({ onClose }: { onClose: () => void }) {
  const t = useT();
  const [files, setFiles] = useState<string[] | null>(cache);
  const [q, setQ] = useState("");
  const [i, setI] = useState(0);
  const input = useRef<HTMLInputElement>(null);
  const list = useRef<HTMLDivElement>(null);
  useEffect(() => { input.current?.focus(); if (!cache) void fs.files().then((r) => { cache = r.files; setFiles(r.files); }); }, []);
  const results = useMemo(() => {
    if (!files) return [];
    const scored: { path: string; score: number; hits: number[] }[] = [];
    for (const f of files) { const m = fuzzy(q, f); if (m) scored.push({ path: f, ...m }); }
    return scored.sort((a, b) => b.score - a.score).slice(0, 60);
  }, [files, q]);
  useEffect(() => { setI(0); }, [q]);
  useEffect(() => { list.current?.children[i]?.scrollIntoView({ block: "nearest" }); }, [i]);
  const open = (path: string) => { onClose(); void editors.open(path); };
  return (
    <div className="overlay top" onMouseDown={(e) => { if (e.target === e.currentTarget) onClose(); }}>
      <div className="palette" role="dialog" aria-modal="true" aria-label={t("qo.title")}>
        <input ref={input} className="field" value={q} placeholder={t("qo.placeholder")} spellCheck={false}
               onChange={(e) => setQ(e.target.value)}
               onKeyDown={(e) => {
                 if (e.key === "Escape") onClose();
                 else if (e.key === "ArrowDown") { e.preventDefault(); setI((x) => Math.min(results.length - 1, x + 1)); }
                 else if (e.key === "ArrowUp") { e.preventDefault(); setI((x) => Math.max(0, x - 1)); }
                 else if (e.key === "Enter" && results[i]) open(results[i]!.path);
               }} />
        <div ref={list} className="pal-list" role="listbox">
          {!files && <div className="tree-msg">{t("loading")}</div>}
          {files && !results.length && <div className="tree-msg">{t("qo.none")}</div>}
          {results.map((r, k) => {
            const name0 = r.path.lastIndexOf("/") + 1, hit = new Set(r.hits);
            const mark = (from: number, to: number) => [...r.path.slice(from, to)].map((ch, n) => hit.has(from + n) ? <b key={n}>{ch}</b> : ch);
            return (
              <div key={r.path} role="option" aria-selected={k === i} className={`pal-item${k === i ? " sel" : ""}`}
                   onMouseEnter={() => setI(k)} onMouseDown={(e) => { e.preventDefault(); open(r.path); }}>
                <FileIcon name={r.path} /><span className="nm">{mark(name0, r.path.length)}</span>
                <span className="dim">{mark(0, Math.max(0, name0 - 1))}</span>
              </div>
            );
          })}
        </div>
      </div>
    </div>
  );
}
