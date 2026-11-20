/* The chat's model picker, in two tiers that never mix:
 *   Free — the free keys of the direct providers (Groq, Gemini, NVIDIA, Cohere): no charge, the provider's limits;
 *   Paid — the user's OpenRouter key, billed per token.
 * The trigger always says which one the next message goes to. */
import { useEffect, useMemo, useRef, useState } from "react";
import { Check, ChevronDown, Coins, Gift, KeyRound, TriangleAlert } from "lucide-react";
import type { ModelChoice } from "../lib/api";
import { useAgent } from "../lib/agent";
import { useT } from "../lib/i18n";
import { fmtPrice, useOpenRouter } from "../lib/openrouter";

export type Tier = "free" | "paid";
export const tierOf = (m: Pick<ModelChoice, "tier" | "paid"> | undefined): Tier => (m?.tier ?? (m?.paid ? "paid" : "free"));

const VENDOR: Record<string, string> = {
  anthropic: "Anthropic", openai: "OpenAI", google: "Google", deepseek: "DeepSeek", qwen: "Qwen", "z-ai": "Z.ai",
  moonshotai: "Moonshot", minimax: "MiniMax", nvidia: "NVIDIA", "x-ai": "xAI", mistralai: "Mistral", meta: "Meta",
};
const PROVIDER: Record<string, string> = { groq: "Groq", gemini: "Google AI Studio", nvidia: "NVIDIA", cohere: "Cohere", ollama: "Ollama" };
const READS: Record<string, string> = { image: "📷", audio: "🎧", video: "🎬", file: "📄" };

const group = (m: ModelChoice) => (m.provider === "openrouter" ? VENDOR[m.model.split("/")[0]!] ?? m.model.split("/")[0]! : PROVIDER[m.provider] ?? m.provider);
const cost = (m: ModelChoice) => (m.price ? m.price.in + m.price.out * 3 : Number.MAX_VALUE);
const shortName = (m: ModelChoice) => (m.provider === "openrouter" ? m.model.split("/").slice(1).join("/") || m.model : m.model.split("/").pop()!);
const k = (n: number) => (n >= 10_000 ? `${Math.round(n / 1000)}k` : n >= 1000 ? `${(n / 1000).toFixed(1)}k` : `${n}`);

/** "990 req · 5.2k tok left", "used up today", or "" before the provider has answered once */
function LiveLimits({ m }: { m: ModelChoice }) {
  const t = useT();
  const l = m.live;
  if (!l) return null;
  if (l.exhausted_at) return <span className="mp-live out"><TriangleAlert size={11} />{t("tier.exhausted")}</span>;
  const parts = [l.requests_left != null ? t("tier.req", { n: k(l.requests_left) }) : "", l.tokens_left != null ? t("tier.tok", { n: k(l.tokens_left) }) : ""].filter(Boolean);
  return parts.length ? <span className="mp-live" title={t("tier.liveTip")}>{t("tier.left", { v: parts.join(" · ") })}</span> : null;
}

export function ModelPicker() {
  const t = useT();
  const ag = useAgent();
  const or = useOpenRouter();
  const [open, setOpen] = useState(false);
  const cur = ag.models.find((m) => ag.model && m.provider === ag.model.provider && m.model === ag.model.model);
  const [tab, setTab] = useState<Tier>(tierOf(cur));
  const root = useRef<HTMLDivElement>(null);
  const list = useRef<HTMLDivElement>(null);

  useEffect(() => { if (open) { setTab(tierOf(cur)); void or.load(); } }, [open]);  // eslint-disable-line react-hooks/exhaustive-deps
  useEffect(() => {
    if (!open) return;
    const away = (e: MouseEvent) => { if (!root.current?.contains(e.target as Node)) setOpen(false); };
    const esc = (e: KeyboardEvent) => { if (e.key === "Escape") { e.stopPropagation(); setOpen(false); } };
    document.addEventListener("mousedown", away);
    document.addEventListener("keydown", esc, true);
    if (list.current) list.current.scrollTop = 0;  // each tab starts at its top, or at the chosen model
    requestAnimationFrame(() => (list.current?.querySelector(".mp-row.sel") as HTMLElement | null)?.scrollIntoView({ block: "nearest" }));
    return () => { document.removeEventListener("mousedown", away); document.removeEventListener("keydown", esc, true); };
  }, [open, tab]);

  const groups = useMemo(() => {
    const g = new Map<string, ModelChoice[]>();
    for (const m of ag.models.filter((x) => tierOf(x) === tab)) g.set(group(m), [...(g.get(group(m)) ?? []), m]);
    const sorted = [...g].map(([name, ms]) => [name, [...ms].sort((a, b) => cost(a) - cost(b) || a.model.localeCompare(b.model))] as const);
    return sorted.sort((a, b) => Math.min(...a[1].map(cost)) - Math.min(...b[1].map(cost)) || a[0].localeCompare(b[0]));
  }, [ag.models, tab]);
  const count = (tier: Tier) => ag.models.filter((m) => tierOf(m) === tier && m.key_ok).length;
  const choose = (m: ModelChoice) => { if (!m.key_ok) return; ag.setModel({ provider: m.provider, model: m.model }); setOpen(false); };
  const tier = tierOf(cur);
  const bal = or.account?.balance ?? or.account?.remaining;

  return (
    <div className="mp" ref={root}>
      <button type="button" className={`mp-trigger t-${tier}`} aria-haspopup="listbox" aria-expanded={open} aria-label={t("ag.model")}
              title={cur ? `${t(tier === "paid" ? "tier.paid" : "tier.free")} · ${cur.model}${tier === "free" && cur.limits ? `\n${cur.limits}` : ""}` : t("ag.model")} onClick={() => setOpen(!open)}>
        <span className={`tier-b t-${tier}`}>{tier === "paid" ? <Coins size={11} /> : <Gift size={11} />}<span className="tier-w">{t(tier === "paid" ? "tier.paid" : "tier.free")}</span></span>
        <span className="mp-name">{cur ? shortName(cur) : "—"}</span>
        <ChevronDown size={12} className="mp-chev" />
      </button>
      {open && (
        <div className="mp-pop" role="dialog" aria-label={t("ag.model")}>
          <div className="mp-tabs" role="tablist">
            {(["free", "paid"] as const).map((x) => (
              <button key={x} type="button" role="tab" aria-selected={tab === x} className={`mp-tab t-${x}`} onClick={() => setTab(x)}>
                {x === "paid" ? <Coins size={14} /> : <Gift size={14} />}
                <span className="mp-tab-t"><b>{t(x === "paid" ? "tier.paid" : "tier.free")}</b><span>{t(x === "paid" ? "tier.paidSub" : "tier.freeSub")}</span></span>
                <span className="n">{count(x)}</span>
              </button>
            ))}
          </div>
          <div className={`mp-about t-${tab}`}>
            <p>{t(tab === "paid" ? "tier.paidAbout" : "tier.freeAbout")}</p>
            {tab === "paid" && (
              <p className="mp-acc">{or.account?.key === false ? t("or.noKey")
                : bal != null ? <>{t("tier.balance")} <b>${fmtPrice(bal)}</b>{or.account?.used != null && <span className="dim"> · {t("or.spent", { v: fmtPrice(or.account.used) })}</span>}</>
                : or.account?.error ?? t("tier.balanceLoading")}</p>
            )}
          </div>
          <div className="mp-list" role="listbox" ref={list} aria-label={t(tab === "paid" ? "tier.paid" : "tier.free")}>
            {!groups.length && <div className="mp-empty">{t(tab === "paid" ? "or.noKey" : "tier.noneFree")}</div>}
            {groups.map(([name, ms]) => (
              <div key={name} className="mp-group">
                <div className="mp-gh">{name}{tab === "free" && ms[0]?.limits && <span className="mp-lim">{ms[0].limits}</span>}</div>
                {ms.map((m) => {
                  const sel = cur === m;
                  return (
                    <button key={m.provider + m.model} type="button" role="option" aria-selected={sel} disabled={!m.key_ok}
                            className={`mp-row${sel ? " sel" : ""}`} title={[m.model, m.notes].filter(Boolean).join("\n")} onClick={() => choose(m)}>
                      <span className="mp-check">{sel && <Check size={13} />}</span>
                      <span className="mp-rn">{shortName(m)}<span className="mp-reads">{(m.inputs ?? []).map((x) => READS[x] ?? "").join("")}</span></span>
                      {!m.key_ok ? <span className="mp-nokey"><KeyRound size={11} />{t("tier.noKey", { k: m.key_env ?? "key" })}</span>
                        : tab === "paid" ? <span className="mp-price">{m.price ? <><b>${fmtPrice(m.price.in)}</b><span className="dim">/</span><b>${fmtPrice(m.price.out)}</b></> : <span className="dim">$</span>}</span>
                        : <LiveLimits m={m} />}
                    </button>
                  );
                })}
              </div>
            ))}
          </div>
          <div className="mp-foot">{t(tab === "paid" ? "tier.paidFoot" : "tier.freeFoot")}</div>
        </div>
      )}
    </div>
  );
}
