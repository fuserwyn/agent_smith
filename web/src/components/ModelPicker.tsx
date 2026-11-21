/* The chat's model picker, in three tiers that never mix:
 *   Free — the free keys of the direct providers (Groq, Gemini, NVIDIA, Cohere): no charge, the provider's limits;
 *   Paid — the user's OpenRouter key, billed per token.
 * The trigger always says which one the next message goes to. */
import { useEffect, useMemo, useRef, useState } from "react";
import { Check, ChevronDown, Coins, Gift, KeyRound, Sparkles, TriangleAlert } from "lucide-react";
import type { ModelChoice } from "../lib/api";
import { useAgent } from "../lib/agent";
import { useT } from "../lib/i18n";
import { fmtPrice, useOpenRouter } from "../lib/openrouter";

export type Tier = "free" | "paid" | "claude";
export const TIERS: Tier[] = ["free", "paid", "claude"];
export const tierOf = (m: Pick<ModelChoice, "tier" | "paid"> | undefined): Tier => (m?.tier ?? (m?.paid ? "paid" : "free"));
/** i18n key of a tier's name, and its icon */
export const tierName = (x: Tier) => (x === "paid" ? "tier.paid" : x === "claude" ? "tier.claude" : "tier.free") as "tier.paid";
export function TierIcon({ tier, size = 11 }: { tier: Tier; size?: number }) {
  return tier === "paid" ? <Coins size={size} /> : tier === "claude" ? <Sparkles size={size} /> : <Gift size={size} />;
}
const TEXT = {
  free: { sub: "tier.freeSub", about: "tier.freeAbout", foot: "tier.freeFoot", empty: "tier.noneFree" },
  paid: { sub: "tier.paidSub", about: "tier.paidAbout", foot: "tier.paidFoot", empty: "or.noKey" },
  claude: { sub: "tier.claudeSub", about: "tier.claudeAbout", foot: "tier.claudeFoot", empty: "tier.claudeNone" },
} as const;

const VENDOR: Record<string, string> = {
  anthropic: "Anthropic", openai: "OpenAI", google: "Google", deepseek: "DeepSeek", qwen: "Qwen", "z-ai": "Z.ai",
  moonshotai: "Moonshot", minimax: "MiniMax", nvidia: "NVIDIA", "x-ai": "xAI", mistralai: "Mistral", meta: "Meta",
};
const PROVIDER: Record<string, string> = { groq: "Groq", gemini: "Google AI Studio", nvidia: "NVIDIA", cohere: "Cohere", ollama: "Ollama" };
const READS: Record<string, string> = { image: "📷", audio: "🎧", video: "🎬", file: "📄" };

const HOST: Record<string, string> = { groq: "Groq", cerebras: "Cerebras", sambanova: "SambaNova", together: "Together" };
/** group of a model in the list: who serves it (and, for OpenRouter, the vendor or the pinned host) */
const groupOf = (m: ModelChoice, t: ReturnType<typeof useT>) => {
  if (m.provider === "claude-code") return `Claude ${(m.family ?? "").replace(/^./, (c) => c.toUpperCase())}`.trim();
  if (m.provider !== "openrouter") return t("tier.freeKey", { p: PROVIDER[m.provider] ?? m.provider });
  const host = m.model.split("@")[1];
  if (host) return t("tier.viaOR", { p: HOST[host] ?? host });
  return VENDOR[m.model.split("/")[0]!] ?? m.model.split("/")[0]!;
};
const cost = (m: ModelChoice) => (m.price ? m.price.in + m.price.out * 3 : Number.MAX_VALUE);
const shortName = (m: ModelChoice) => {
  if (m.provider === "claude-code") return m.label ?? m.model;
  const [id, host] = m.model.split("@");
  const name = m.provider === "openrouter" ? id!.split("/").slice(1).join("/") || id! : id!.split("/").pop()!;
  return host ? `${name} · ${HOST[host] ?? host}` : name;
};
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
    for (const m of ag.models.filter((x) => tierOf(x) === tab)) g.set(groupOf(m, t), [...(g.get(groupOf(m, t)) ?? []), m]);
    const sorted = [...g].map(([name, ms]) => [name, [...ms].sort((a, b) => (a.provider === "claude-code" ? 0 : cost(a) - cost(b) || a.model.localeCompare(b.model)))] as const);
    if (tab === "claude") return sorted;  // families in their order (Fable, Opus, Sonnet, Haiku), newest version first
    return sorted.sort((a, b) => Math.min(...a[1].map(cost)) - Math.min(...b[1].map(cost)) || a[0].localeCompare(b[0]));
  }, [ag.models, tab, t]);
  const count = (tier: Tier) => ag.models.filter((m) => tierOf(m) === tier && m.key_ok).length;
  const choose = (m: ModelChoice) => { if (!m.key_ok) return; ag.setModel({ provider: m.provider, model: m.model }); setOpen(false); };
  const tier = tierOf(cur);
  const bal = or.account?.balance ?? or.account?.remaining;

  return (
    <div className="mp" ref={root}>
      <button type="button" className={`mp-trigger t-${tier}`} aria-haspopup="listbox" aria-expanded={open} aria-label={t("ag.model")}
              title={cur ? `${t(tierName(tier))} · ${cur.model}${tier === "free" && cur.limits ? `\n${cur.limits}` : ""}` : t("ag.model")} onClick={() => setOpen(!open)}>
        <span className={`tier-b t-${tier}`}><TierIcon tier={tier} /><span className="tier-w">{t(tierName(tier))}</span></span>
        <span className="mp-name">{cur ? shortName(cur) : "—"}</span>
        <ChevronDown size={12} className="mp-chev" />
      </button>
      {open && (
        <div className="mp-pop" role="dialog" aria-label={t("ag.model")}>
          <div className="mp-tabs" role="tablist">
            {TIERS.map((x) => (
              <button key={x} type="button" role="tab" aria-selected={tab === x} className={`mp-tab t-${x}`} onClick={() => setTab(x)}>
                <TierIcon tier={x} size={14} />
                <span className="mp-tab-t"><b>{t(x === "claude" ? "tier.claudeTab" : tierName(x))}</b><span>{t(TEXT[x].sub)}</span></span>
                <span className="n">{count(x)}</span>
              </button>
            ))}
          </div>
          <div className={`mp-about t-${tab}`}>
            <p>{t(TEXT[tab].about)}</p>
            {tab === "paid" && (
              <p className="mp-acc">{or.account?.key === false ? t("or.noKey")
                : bal != null ? <>{t("tier.balance")} <b>${fmtPrice(bal)}</b>{or.account?.used != null && <span className="dim"> · {t("or.spent", { v: fmtPrice(or.account.used) })}</span>}</>
                : or.account?.error ?? t("tier.balanceLoading")}</p>
            )}
          </div>
          <div className="mp-list" role="listbox" ref={list} aria-label={t(tierName(tab))}>
            {!groups.length && <div className="mp-empty">{t(TEXT[tab].empty)}</div>}
            {groups.map(([name, ms]) => (
              <div key={name} className="mp-group">
                <div className="mp-gh">{name}
                  {tab === "free" && ag.models.some((x) => x.provider === "openrouter" && x.model.endsWith("@" + ms[0]!.provider)) && (
                    <button type="button" className="mp-jump" onClick={() => setTab("paid")}>{t("tier.samePaid")}</button>)}
                  {tab === "free" && ms[0]?.limits && <span className="mp-lim">{ms[0].limits}</span>}</div>
                {ms.map((m) => {
                  const sel = cur === m;
                  return (
                    <button key={m.provider + m.model} type="button" role="option" aria-selected={sel} disabled={!m.key_ok}
                            className={`mp-row${sel ? " sel" : ""}`} title={[m.model, m.notes].filter(Boolean).join("\n")} onClick={() => choose(m)}>
                      <span className="mp-check">{sel && <Check size={13} />}</span>
                      <span className="mp-rn">{shortName(m)}<span className="mp-reads">{(m.inputs ?? []).map((x) => READS[x] ?? "").join("")}</span></span>
                      {!m.key_ok ? <span className="mp-nokey"><KeyRound size={11} />{t("tier.noKey", { k: m.key_env ?? "key" })}</span>
                        : tab === "claude" ? <span className="mp-plan">{t("tier.inPlan")}</span>
                        : tab === "paid" ? <span className="mp-price">{m.price ? <><b>${fmtPrice(m.price.in)}</b><span className="dim">/</span><b>${fmtPrice(m.price.out)}</b></> : <span className="dim">$</span>}</span>
                        : <LiveLimits m={m} />}
                    </button>
                  );
                })}
              </div>
            ))}
          </div>
          <div className="mp-foot">{t(TEXT[tab].foot)}</div>
        </div>
      )}
    </div>
  );
}
