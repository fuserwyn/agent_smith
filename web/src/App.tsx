import { useCallback, useEffect, useRef, useState } from "react";
import { KeyRound, LoaderCircle, RefreshCw, ServerOff } from "lucide-react";
import { Workbench } from "./components/Workbench";
import { StatusBar, type Conn } from "./components/StatusBar";
import { EmptyState } from "./components/ui";
import { api, ApiError, hasToken, type Session } from "./lib/api";
import { LANGS, LangContext, type Lang, translate } from "./lib/i18n";
import { usePref, useTheme } from "./lib/prefs";

const HEALTH_EVERY_MS = 10_000;

export default function App() {
  const browserLang = navigator.language.slice(0, 2) as Lang;
  const [lang, setLang] = usePref<Lang>("studio-lang", LANGS.includes(browserLang) ? browserLang : "en", LANGS);
  const [theme, setTheme] = useTheme();
  const [conn, setConn] = useState<Conn>("connecting");
  const [session, setSession] = useState<Session | null>(null);
  const [unauthorized, setUnauthorized] = useState(!hasToken());
  const connRef = useRef(conn);
  connRef.current = conn;

  const load = useCallback(async () => {
    try {
      setSession(await api<Session>("/api/session"));
      setConn("connected");
    } catch (e) {
      if (e instanceof ApiError && e.status === 401) setUnauthorized(true);
      setConn("offline");
    }
  }, []);

  useEffect(() => {
    if (unauthorized) return;
    void load();
    const id = window.setInterval(() => {  // light heartbeat: shows when the server stops, recovers by itself
      api("/api/health").then(() => { if (connRef.current !== "connected") void load(); }).catch((e) => {
        if (e instanceof ApiError && e.status === 401) setUnauthorized(true);
        setConn("offline");
      });
    }, HEALTH_EVERY_MS);
    return () => window.clearInterval(id);
  }, [load, unauthorized]);

  useEffect(() => { document.documentElement.lang = lang; document.title = translate(lang, "app.name"); }, [lang]);

  const t = (k: Parameters<typeof translate>[1]) => translate(lang, k);
  let body;
  if (unauthorized) {
    body = (
      <div className="gate">
        <EmptyState icon={<KeyRound size={28} />} title={t("token.title")} body={t("token.body")} />
        <div className="gate-cmd"><span>{t("token.cmd")}</span><code>uv run --extra studio agent-studio .</code></div>
      </div>
    );
  } else if (conn === "offline" && !session) {
    body = (
      <div className="gate">
        <EmptyState icon={<ServerOff size={28} />} title={t("offline.title")} body={t("offline.body")} />
        <button type="button" className="btn" onClick={() => { setConn("connecting"); void load(); }}><RefreshCw size={14} />{t("retry")}</button>
      </div>
    );
  } else if (!session) body = <div className="gate"><LoaderCircle className="spin muted" size={20} /></div>;
  else body = <Workbench workspace={session.workspace.path} />;

  return (
    <LangContext.Provider value={lang}>
      <div className="app">
        {body}
        <StatusBar conn={unauthorized ? "locked" : conn} session={session} theme={theme} onTheme={setTheme} lang={lang} onLang={setLang} />
      </div>
    </LangContext.Provider>
  );
}
