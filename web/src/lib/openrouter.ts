/* OpenRouter tab state: catalog (loaded once), balance, picks. Shared by the side panel and the model browser. */
import { useSyncExternalStore } from "react";
import { openrouterApi, type MediaKind, type OrAccount, type OrModel, type OrPicks } from "./api";

class OpenRouterStore {
  models: OrModel[] = [];
  byId = new Map<string, OrModel>();
  picks: OrPicks = { chat: [], media: {} };
  defaults: Partial<Record<MediaKind, string[]>> = {};
  account: OrAccount | null = null;
  loading = false;
  error: string | null = null;
  private v = 0;
  private subs = new Set<() => void>();
  subscribe = (cb: () => void) => { this.subs.add(cb); return () => { this.subs.delete(cb); }; };
  getV = () => this.v;
  emit() { this.v++; this.subs.forEach((s) => s()); }

  async load(force = false) {
    if (this.loading || (this.models.length && !force)) return;
    this.loading = true; this.error = null; this.emit();
    try {
      const [c, a] = await Promise.all([openrouterApi.catalog(), openrouterApi.account().catch(() => null)]);
      this.models = c.models; this.byId = new Map(c.models.map((m) => [m.id, m]));
      this.picks = c.picks; this.defaults = c.media_defaults; this.account = a;
    } catch (e) { this.error = e instanceof Error ? e.message : String(e); }
    this.loading = false; this.emit();
  }

  /** the model used for a media kind: the user's pick, else the first one of config/models.json */
  mediaModel(kind: MediaKind): string | undefined { return this.picks.media[kind] ?? this.defaults[kind]?.[0]; }

  private async save(p: OrPicks) { this.picks = p; this.emit(); this.picks = await openrouterApi.savePicks(p); this.emit(); }
  toggleChat(id: string) {
    const has = this.picks.chat.includes(id);
    return this.save({ ...this.picks, chat: has ? this.picks.chat.filter((x) => x !== id) : [...this.picks.chat, id] });
  }
  setMedia(kind: MediaKind, id: string | null) {
    const media = { ...this.picks.media };
    if (id) media[kind] = id; else delete media[kind];
    return this.save({ ...this.picks, media });
  }
}

export const openrouter = new OpenRouterStore();
export function useOpenRouter(): OpenRouterStore {
  useSyncExternalStore(openrouter.subscribe, openrouter.getV);
  return openrouter;
}

/** which media roles a model can fill */
export function rolesOf(m: OrModel): MediaKind[] {
  const r: MediaKind[] = [];
  if (m.kinds.includes("embeddings")) return ["embeddings"];
  if (m.out.includes("image")) r.push("image");
  if (m.out.includes("video")) r.push("video");
  if (m.out.includes("audio")) r.push("speech");
  if (m.in.includes("audio") && m.out.includes("text")) r.push("transcribe");
  if (m.out.includes("text") && m.in.some((k) => k === "image" || k === "video" || k === "audio" || k === "file")) r.push("perceive");
  return r;
}

export const fmtPrice = (v: number | null | undefined) =>
  v == null ? "—" : v === 0 ? "0" : v < 0.01 ? v.toFixed(4).replace(/0+$/, "") : v < 1 ? v.toFixed(3).replace(/0+$/, "").replace(/\.$/, "") : v.toFixed(2).replace(/\.00$/, "");
export const vendorOf = (id: string) => id.split("/")[0] ?? id;
export const fmtCtx = (n: number) => (n >= 1_000_000 ? `${(n / 1_000_000).toFixed(n % 1_000_000 ? 1 : 0)}M` : n >= 1000 ? `${Math.round(n / 1000)}K` : `${n}`);
