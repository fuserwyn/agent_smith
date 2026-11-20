/* "Apps" bottom tab: the projects the agent built and runs in Docker (run_app) — URL, status, logs, stop. */
import { useEffect, useMemo, useState } from "react";
import { Boxes, ExternalLink, RefreshCw, Square } from "lucide-react";
import { useAgent } from "../lib/agent";
import { appsApi, type AppInfo } from "../lib/api";
import { useT } from "../lib/i18n";
import { EmptyState } from "./ui";

export function AppLink({ app }: { app: AppInfo }) {
  return app.status === "running"
    ? <a className="app-url mono" href={app.url} target="_blank" rel="noreferrer">{app.url.replace("http://", "")}<ExternalLink size={11} /></a>
    : <span className="app-url mono off">{app.url.replace("http://", "")}</span>;
}

export function AppsPanel({ visible, request }: { visible: boolean; request: { id: string; n: number } | null }) {
  const t = useT();
  const ag = useAgent();
  const apps = useMemo(() => Object.values(ag.apps).sort((a, b) => b.started - a.started), [ag.apps]);
  const [sel, setSel] = useState<string | null>(null);
  const [logs, setLogs] = useState("");
  const [tick, setTick] = useState(0);
  useEffect(() => { if (request) setSel(request.id); }, [request]);
  useEffect(() => { if (!sel && apps[0]) setSel(apps[0].id); }, [sel, apps]);
  const app = sel ? ag.apps[sel] : undefined;
  useEffect(() => {  // logs of the selected app, refreshed while it runs and the tab is visible
    if (!visible || !app) return;
    let stop = false;
    const load = () => appsApi.logs(app.id).then((r) => { if (!stop) setLogs(r.logs); }).catch(() => undefined);
    void load();
    const id = app.status === "running" ? window.setInterval(load, 2000) : 0;
    return () => { stop = true; window.clearInterval(id); };
  }, [visible, app?.id, app?.status, tick]);  // eslint-disable-line react-hooks/exhaustive-deps

  if (!apps.length) return <EmptyState icon={<Boxes size={22} />} title={t("apps.none")} body={t("apps.noneBody")} />;
  return (
    <div className="apps">
      <div className="apps-list" role="listbox" aria-label={t("panel.output")}>
        {apps.map((a) => (
          <div key={a.id} role="option" aria-selected={a.id === sel} className={`app-row${a.id === sel ? " sel" : ""}`} onClick={() => setSel(a.id)}>
            <span className={`app-dot s-${a.status}`} title={t(`apps.${a.status}` as "apps.running")} />
            <span className="app-main"><span className="nm">{a.name}</span><span className="dim mono">{a.path}</span></span>
            <AppLink app={a} />
          </div>
        ))}
      </div>
      {app && (
        <div className="app-logs">
          <div className="app-logs-h">
            <span className="muted">{t("apps.logs")} · <span className="mono">{app.container}</span>{app.error ? ` · ${app.error}` : ""}</span>
            <span className="sp" />
            <button type="button" className="icon-btn" title={t("apps.refresh")} aria-label={t("apps.refresh")} onClick={() => setTick((n) => n + 1)}><RefreshCw size={13} /></button>
            {app.status === "running" && (
              <button type="button" className="btn sm" onClick={() => void ag.stopApp(app.id)}><Square size={12} />{t("apps.stop")}</button>
            )}
          </div>
          <pre className="app-logs-b">{logs || t("apps.noLogs")}</pre>
        </div>
      )}
    </div>
  );
}
