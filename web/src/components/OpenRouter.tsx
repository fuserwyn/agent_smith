/* OpenRouter tab: one key, the whole catalog. Side panel (balance, picks, media defaults) + a full-width model
 * browser (search, kinds, "reads" filters, sort, detail card with the actions). */
import { useEffect, useMemo, useRef, useState } from "react";
import {
  AudioLines, Binary, Check, Clapperboard, Copy, Eye, FileText, Image as ImageIcon, Mic, MessageSquare, Plus, RefreshCw,
  Search, Sparkles, Wallet, Wrench, X,
} from "lucide-react";
import type { MediaKind, OrModel } from "../lib/api";
import { useT } from "../lib/i18n";
import { fmtCtx, fmtPrice, openrouter, rolesOf, useOpenRouter, vendorOf } from "../lib/openrouter";
import { toast } from "../lib/toast";
import { PaneHeader } from "./ui";

const ROLE_ICON: Record<MediaKind, typeof ImageIcon> = {
  image: ImageIcon, video: Clapperboard, speech: AudioLines, transcribe: Mic, perceive: Eye, embeddings: Binary,
};
const ROLES: MediaKind[] = ["image", "video", "speech", "transcribe", "perceive", "embeddings"];
type Kind = "all" | "text" | "image" | "video" | "audio" | "embeddings";
const KINDS: Kind[] = ["all", "text", "image", "video", "audio", "embeddings"];  // paid models only: no free tier

function hue(s: string) { let h = 0; for (const c of s) h = (h * 31 + c.charCodeAt(0)) % 360; return h; }
function Avatar({ id, size = 30 }: { id: string; size?: number }) {
  const v = vendorOf(id);
  return <span className="or-av" style={{ width: size, height: size, ["--h" as string]: hue(v) }}>{v.slice(0, 1).toUpperCase()}</span>;
}
const short = (id?: string) => (id ? id.split("/").slice(1).join("/") || id : "—");

/* ------------------------------------------------------------------ side panel */
export function OpenRouterPanel({ onBrowse }: { onBrowse: (kind?: Kind) => void }) {
  const t = useT();
  const or = useOpenRouter();
  useEffect(() => { void or.load(); }, []);  // eslint-disable-line react-hooks/exhaustive-deps
  const a = or.account;
  return (
    <div className="sidepanel">
      <PaneHeader title="OpenRouter">
        <button type="button" className="icon-btn" title={t("or.refresh")} aria-label={t("or.refresh")} onClick={() => void or.load(true)}>
          <RefreshCw size={14} className={or.loading ? "spin" : ""} /></button>
      </PaneHeader>
      <div className="or-side">
        <div className="or-balance">
          <Wallet size={16} />
          {!a ? <span className="muted">{or.loading ? "…" : t("or.noAccount")}</span> : !a.key ? <span className="muted">{t("or.noKey")}</span> : (
            <div className="or-bal-main">
              <span className="or-bal-v">{a.balance != null ? `$${fmtPrice(a.balance)}` : a.remaining != null ? `$${fmtPrice(a.remaining)}` : "—"}</span>
              <span className="muted">{t("or.balance")}{a.usage != null ? ` · ${t("or.spent", { v: fmtPrice(a.usage) })}` : ""}</span>
            </div>
          )}
        </div>
        <button type="button" className="or-browse" onClick={() => onBrowse()}>
          <Sparkles size={16} /><span><b>{t("or.browse")}</b><span className="muted">{t("or.browseSub", { n: or.models.length || "500+" })}</span></span>
        </button>
        <section>
          <h4><MessageSquare size={12} />{t("or.chat")}</h4>
          {!or.picks.chat.length ? <p className="or-hint">{t("or.chatHint")}</p> : (
            <div className="or-chips">
              {or.picks.chat.map((id) => (
                <span key={id} className="or-chip" title={id}><Avatar id={id} size={16} /><span className="nm">{short(id)}</span>
                  <button type="button" aria-label={t("or.remove")} onClick={() => void or.toggleChat(id)}><X size={11} /></button></span>
              ))}
            </div>
          )}
        </section>
        <section>
          <h4><Wrench size={12} />{t("or.defaults")}</h4>
          <div className="or-roles">
            {ROLES.map((r) => {
              const I = ROLE_ICON[r];
              const id = or.mediaModel(r);
              const mine = !!or.picks.media[r];
              return (
                <button key={r} type="button" className="or-role" onClick={() => onBrowse(r === "embeddings" ? "embeddings" : r === "image" ? "image" : r === "video" ? "video" : r === "speech" || r === "transcribe" ? "audio" : "all")}>
                  <span className={`or-role-i r-${r}`}><I size={14} /></span>
                  <span className="or-role-t"><span>{t(`or.role.${r}` as "or.role.image")}</span><span className="mono dim">{short(id)}{mine ? " ·  ★" : ""}</span></span>
                </button>
              );
            })}
          </div>
        </section>
      </div>
    </div>
  );
}

/* ------------------------------------------------------------------ full-width browser */
export function ModelBrowser({ initialKind = "all", onClose }: { initialKind?: Kind; onClose: () => void }) {
  const t = useT();
  const or = useOpenRouter();
  const [q, setQ] = useState("");
  const [kind, setKind] = useState<Kind>(initialKind);
  const [reads, setReads] = useState<string[]>([]);
  const [sort, setSort] = useState<"new" | "cheap" | "context">("new");
  const [sel, setSel] = useState<string | null>(null);
  const search = useRef<HTMLInputElement>(null);
  useEffect(() => { void or.load(); search.current?.focus(); }, []);  // eslint-disable-line react-hooks/exhaustive-deps
  useEffect(() => {
    const esc = (e: KeyboardEvent) => { if (e.key === "Escape") { if (sel) setSel(null); else onClose(); } };
    window.addEventListener("keydown", esc);
    return () => window.removeEventListener("keydown", esc);
  }, [sel, onClose]);

  const list = useMemo(() => {
    const words = q.toLowerCase().split(/\s+/).filter(Boolean);
    let ms = or.models.filter((m) =>
      (kind === "all" || m.kinds.includes(kind as "text")) &&
      reads.every((r) => m.in.includes(r)) &&
      words.every((w) => `${m.id} ${m.name}`.toLowerCase().includes(w)));
    const cost = (m: OrModel) => (m.price.in ?? 0) + (m.price.out ?? 0) * 3 + (m.price.image_out ?? 0);
    if (sort === "cheap") ms = [...ms].sort((a, b) => cost(a) - cost(b));
    else if (sort === "context") ms = [...ms].sort((a, b) => b.context - a.context);
    return ms;
  }, [or.models, q, kind, reads, sort]);
  const counts = useMemo(() => Object.fromEntries(KINDS.map((k) => [k, k === "all" ? or.models.length : or.models.filter((m) => m.kinds.includes(k as "text")).length])), [or.models]);
  const m = sel ? or.byId.get(sel) : undefined;

  return (
    <div className="or-page" role="dialog" aria-label={t("or.browse")}>
      <header className="or-top">
        <div className="or-title"><Sparkles size={18} /><h2>{t("or.browse")}</h2><span className="muted">{t("or.count", { n: list.length, all: or.models.length })}</span></div>
        <div className="or-search"><Search size={15} /><input ref={search} value={q} onChange={(e) => setQ(e.target.value)} placeholder={t("or.search")} spellCheck={false} /></div>
        <select className="model-sel or-sort" value={sort} onChange={(e) => setSort(e.target.value as typeof sort)} aria-label={t("or.sort")}>
          <option value="new">{t("or.sort.new")}</option><option value="cheap">{t("or.sort.cheap")}</option><option value="context">{t("or.sort.context")}</option>
        </select>
        <button type="button" className="icon-btn" onClick={onClose} title={t("or.close")} aria-label={t("or.close")}><X size={16} /></button>
      </header>
      <div className="or-filters">
        <div className="or-kinds" role="tablist">
          {KINDS.map((k) => (
            <button key={k} type="button" role="tab" aria-selected={kind === k} className="or-kind" onClick={() => setKind(k)}>
              {t(`or.kind.${k}` as "or.kind.all")}<span className="n">{counts[k] ?? 0}</span>
            </button>
          ))}
        </div>
        <span className="sp" />
        <span className="muted or-reads-l">{t("or.reads")}</span>
        {(["image", "audio", "video", "file"] as const).map((r) => {
          const I = r === "image" ? ImageIcon : r === "audio" ? AudioLines : r === "video" ? Clapperboard : FileText;
          return <button key={r} type="button" className="or-read" aria-pressed={reads.includes(r)} title={t(`or.read.${r}` as "or.read.image")}
                         onClick={() => setReads((x) => x.includes(r) ? x.filter((y) => y !== r) : [...x, r])}><I size={14} /></button>;
        })}
      </div>
      <div className={`or-body${m ? " with-detail" : ""}`}>
        <div className="or-grid">
          {or.loading && !or.models.length && <div className="or-empty"><RefreshCw size={18} className="spin" />{t("or.loading")}</div>}
          {or.error && <div className="or-empty">{or.error}</div>}
          {!or.loading && !list.length && !or.error && <div className="or-empty">{t("or.none")}</div>}
          {list.slice(0, 400).map((x) => <Card key={x.id} m={x} selected={x.id === sel} onClick={() => setSel(x.id === sel ? null : x.id)} />)}
        </div>
        {m && <Detail m={m} onClose={() => setSel(null)} />}
      </div>
    </div>
  );
}

function Badges({ m }: { m: OrModel }) {
  const t = useT();
  const or = useOpenRouter();
  const roles = ROLES.filter((r) => or.mediaModel(r) === m.id);
  return (
    <>
      {or.picks.chat.includes(m.id) && <span className="or-badge chat"><MessageSquare size={10} />{t("or.inChat")}</span>}
      {roles.map((r) => <span key={r} className="or-badge role">{t(`or.role.${r}` as "or.role.image")}</span>)}
    </>
  );
}

function Card({ m, selected, onClick }: { m: OrModel; selected: boolean; onClick: () => void }) {
  const t = useT();
  return (
    <button type="button" className={`or-card${selected ? " sel" : ""}`} onClick={onClick}>
      <div className="or-card-h"><Avatar id={m.id} /><span className="or-card-t"><b>{m.name.replace(/^[^:]+:\s*/, "")}</b><span className="mono dim">{m.id}</span></span></div>
      <div className="or-card-p">
        {m.kinds.includes("embeddings")
          ? <span><b>${fmtPrice(m.price.in)}</b> <span className="dim">{t("or.perM")}</span></span>
          : <span><b>${fmtPrice(m.price.in)}</b><span className="dim"> / </span><b>${fmtPrice(m.price.out)}</b> <span className="dim">{t("or.perM")}</span></span>}
        {m.price.image_out ? <span className="dim">🖼 ${fmtPrice(m.price.image_out)}</span> : null}
        {m.context ? <span className="dim">{fmtCtx(m.context)}</span> : null}
      </div>
      <div className="or-card-f">
        <span className="or-io">{m.in.filter((x) => x !== "text").map((x) => <i key={x} title={`${t("or.reads")} ${x}`}>{({ image: "📷", audio: "🎧", video: "🎬", file: "📄" } as Record<string, string>)[x] ?? ""}</i>)}
          {m.out.filter((x) => x !== "text").map((x) => <i key={"o" + x} className="out" title={`→ ${x}`}>{({ image: "→🖼", audio: "→🔊", video: "→🎞", embeddings: "→ ⋯" } as Record<string, string>)[x] ?? ""}</i>)}</span>
        <span className="sp" /><Badges m={m} />
      </div>
    </button>
  );
}

function Detail({ m, onClose }: { m: OrModel; onClose: () => void }) {
  const t = useT();
  const or = useOpenRouter();
  const inChat = or.picks.chat.includes(m.id);
  const roles = rolesOf(m);
  const canChat = m.out.includes("text") && !m.kinds.includes("embeddings");
  return (
    <aside className="or-detail">
      <div className="or-detail-h"><Avatar id={m.id} size={40} />
        <div><h3>{m.name.replace(/^[^:]+:\s*/, "")}</h3><span className="mono dim">{m.id}</span></div>
        <button type="button" className="icon-btn" onClick={onClose} aria-label={t("or.close")}><X size={15} /></button></div>
      <div className="or-stats">
        <div><span className="dim">{t("or.in")}</span><b>${fmtPrice(m.price.in)}</b></div>
        {!m.kinds.includes("embeddings") && <div><span className="dim">{t("or.out")}</span><b>${fmtPrice(m.price.out)}</b></div>}
        {m.price.image_out ? <div><span className="dim">{t("or.imgOut")}</span><b>${fmtPrice(m.price.image_out)}</b></div> : null}
        <div><span className="dim">{t("or.context")}</span><b>{m.context ? fmtCtx(m.context) : "—"}</b></div>
      </div>
      <p className="dim or-unit">{t("or.unit")}</p>
      <div className="or-mods">
        <span className="dim">{t("or.reads")}</span>{m.in.map((x) => <span key={x} className="or-mod">{x}</span>)}
        <span className="dim">{t("or.makes")}</span>{m.out.map((x) => <span key={x} className="or-mod out">{x}</span>)}
        {m.tools && <span className="or-mod tools">{t("or.tools")}</span>}
      </div>
      {m.description && <p className="or-desc">{m.description}</p>}
      <div className="or-actions">
        {canChat && (
          <button type="button" className={`btn ${inChat ? "" : "primary"}`} onClick={() => void or.toggleChat(m.id).then(() => toast(t(inChat ? "or.removed" : "or.added", { m: short(m.id) }), "ok"))}>
            {inChat ? <><Check size={14} />{t("or.inChat")}</> : <><Plus size={14} />{t("or.addChat")}</>}
          </button>
        )}
        {roles.map((r) => {
          const I = ROLE_ICON[r];
          const on = or.mediaModel(r) === m.id;
          return (
            <button key={r} type="button" className="btn" aria-pressed={on} disabled={on}
                    onClick={() => void or.setMedia(r, m.id).then(() => toast(t("or.roleSet", { r: t(`or.role.${r}` as "or.role.image"), m: short(m.id) }), "ok"))}>
              <I size={14} />{on ? t("or.isDefault", { r: t(`or.role.${r}` as "or.role.image") }) : t("or.makeDefault", { r: t(`or.role.${r}` as "or.role.image") })}
            </button>
          );
        })}
        <button type="button" className="btn ghost" onClick={() => void navigator.clipboard?.writeText(m.id).then(() => toast(t("or.copied"), "ok"))}><Copy size={14} />{t("or.copy")}</button>
      </div>
    </aside>
  );
}

export { openrouter };
