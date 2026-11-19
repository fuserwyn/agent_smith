/* Search, source control, and placeholders for the views of later stages. */
import { useEffect, useMemo, useRef, useState } from "react";
import { ArrowDown, ArrowUp, CaseSensitive, GitBranch, LoaderCircle, Regex, RotateCw, Search } from "lucide-react";
import { fs, type GitStatus, type SearchHit } from "../lib/api";
import { editors } from "../lib/editors";
import { useT } from "../lib/i18n";
import { FileIcon } from "./fileIcon";
import { EmptyState, IconButton, PaneHeader } from "./ui";

export function SearchPanel({ focusSignal }: { focusSignal: number }) {
  const t = useT();
  const [q, setQ] = useState("");
  const [regex, setRegex] = useState(false);
  const [cased, setCased] = useState(false);
  const [res, setRes] = useState<{ results: SearchHit[]; truncated: boolean; files: number } | null>(null);
  const [err, setErr] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);
  const input = useRef<HTMLInputElement>(null);
  const seq = useRef(0);
  useEffect(() => { input.current?.focus(); input.current?.select(); }, [focusSignal]);
  useEffect(() => {
    if (!q.trim()) { setRes(null); setErr(null); return; }
    const my = ++seq.current;
    const id = window.setTimeout(() => {
      setBusy(true);
      fs.search(q, regex, cased).then((r) => { if (my === seq.current) { setRes(r); setErr(null); } })
        .catch((e) => { if (my === seq.current) { setRes(null); setErr(e instanceof Error ? e.message : String(e)); } })
        .finally(() => { if (my === seq.current) setBusy(false); });
    }, 280);
    return () => window.clearTimeout(id);
  }, [q, regex, cased]);
  const groups = useMemo(() => {
    const m = new Map<string, SearchHit[]>();
    for (const h of res?.results ?? []) m.set(h.path, [...(m.get(h.path) ?? []), h]);
    return [...m];
  }, [res]);
  return (
    <div className="sidepanel">
      <PaneHeader title={t("act.search")}>{busy && <LoaderCircle size={14} className="spin muted" />}</PaneHeader>
      <div className="search-box">
        <Search size={14} className="muted" />
        <input ref={input} className="field bare" value={q} onChange={(e) => setQ(e.target.value)} placeholder={t("sr.placeholder")} spellCheck={false} aria-label={t("act.search")} />
        <IconButton label={t("sr.case")} pressed={cased} onClick={() => setCased(!cased)}><CaseSensitive size={15} /></IconButton>
        <IconButton label={t("sr.regex")} pressed={regex} onClick={() => setRegex(!regex)}><Regex size={14} /></IconButton>
      </div>
      {err && <div className="tree-msg err">{err}</div>}
      {res && <div className="sr-sum">{res.results.length ? t("sr.summary", { n: res.results.length, f: res.files }) : t("sr.none")}{res.truncated ? ` · ${t("sr.truncated")}` : ""}</div>}
      <div className="sr-list">
        {groups.map(([path, hits]) => (
          <div key={path} className="sr-group">
            <div className="sr-file" title={path}><FileIcon name={path} /><span className="nm">{path.split("/").pop()}</span><span className="dim">{path.includes("/") ? path.slice(0, path.lastIndexOf("/")) : ""}</span><span className="badge">{hits.length}</span></div>
            {hits.map((h, i) => (
              <button key={i} type="button" className="sr-hit mono" onClick={() => void editors.open(h.path, { preview: true, line: h.line, col: h.col })}>
                <span className="ln">{h.line}</span>
                <span className="tx">{h.text.slice(0, h.offset)}<mark>{h.text.slice(h.offset, h.offset + h.len)}</mark>{h.text.slice(h.offset + h.len)}</span>
              </button>
            ))}
          </div>
        ))}
      </div>
    </div>
  );
}

const LETTER: Record<string, string> = { modified: "M", added: "A", deleted: "D", renamed: "R", copied: "C", conflict: "!", untracked: "U" };

export function GitPanel({ status, refresh }: { status: GitStatus | null; refresh: () => void }) {
  const t = useT();
  return (
    <div className="sidepanel">
      <PaneHeader title={t("act.git")}><IconButton label={t("fx.refresh")} onClick={refresh}><RotateCw size={14} /></IconButton></PaneHeader>
      {!status ? <div className="tree-msg">{t("loading")}</div>
        : !status.repo ? <EmptyState icon={<GitBranch size={22} />} title={t("git.noRepo")} body={t("git.noRepoBody")} />
        : (
          <>
            <div className="git-branch"><GitBranch size={14} /><span className="mono">{status.branch ?? "—"}</span>
              {!!status.ahead && <span className="ab"><ArrowUp size={12} />{status.ahead}</span>}
              {!!status.behind && <span className="ab"><ArrowDown size={12} />{status.behind}</span>}
            </div>
            <div className="sr-sum">{status.files.length ? t("git.changes", { n: status.files.length }) : t("git.clean")}</div>
            <div className="sr-list">
              {status.files.map((f) => (
                <button key={f.path} type="button" className={`git-row g-${f.status}`} title={`${f.path} — ${t(`git.${f.status}`)}`}
                        onClick={() => void editors.open(f.path, { kind: "git", preview: true })}>
                  <FileIcon name={f.path} /><span className="nm">{f.path.split("/").pop()}</span>
                  <span className="dim">{f.path.includes("/") ? f.path.slice(0, f.path.lastIndexOf("/")) : ""}</span>
                  <span className="gs">{LETTER[f.status]}</span>
                </button>
              ))}
            </div>
          </>
        )}
    </div>
  );
}
