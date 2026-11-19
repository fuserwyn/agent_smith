/* Minimal i18n: one flat dictionary per language, {param} substitution, React context. */
import { createContext, useContext } from "react";

export type Lang = "en" | "ru" | "fr";
export const LANGS: readonly Lang[] = ["en", "ru", "fr"];

const en = {
  "app.name": "Agent Smith Studio",
  "act.explorer": "Explorer",
  "act.search": "Search",
  "act.git": "Source control",
  "act.runs": "Agent runs",
  "act.settings": "Settings",
  "panel.explorer": "Explorer",
  "panel.agent": "Agent",
  "panel.terminal": "Terminal",
  "panel.inside": "Inside the agent",
  "panel.output": "Output",
  "editor.empty.title": "Open a file to start",
  "editor.empty.body": "Pick a file in the Explorer, or ask the agent to create one.",
  "explorer.soon": "The file tree arrives in stage 1.",
  "agent.empty.title": "Ask the agent",
  "agent.empty.body": "Describe a change in plain words. You will see every step it takes, and review each edit before it lands.",
  "agent.soon": "Chat arrives in stage 3.",
  "terminal.soon": "A real terminal (in the project's Docker container) arrives in stage 2.",
  "inside.soon": "The agent visualizer (canvas, interpreter, tool calls) arrives in stage 4.",
  "status.connected": "Connected",
  "status.connecting": "Connecting…",
  "status.offline": "Server unreachable",
  "status.locked": "No session key",
  "status.docker.ok": "Docker",
  "status.docker.off": "Docker stopped",
  "status.docker.none": "No Docker",
  "status.git": "git",
  "theme.system": "System theme",
  "theme.dark": "Dark theme",
  "theme.light": "Light theme",
  "token.title": "This page needs its session key",
  "token.body": "For safety, Agent Smith Studio only answers the link printed in the terminal when it starts. Open that link (it contains ?token=…).",
  "token.cmd": "Start the studio",
  "offline.title": "Cannot reach the studio server",
  "offline.body": "Is agent-studio still running? Start it again, then reload this page.",
  "retry": "Retry",
};
export type Key = keyof typeof en;
type Dict = Record<Key, string>;

const ru: Dict = {
  "app.name": "Agent Smith Studio",
  "act.explorer": "Проводник",
  "act.search": "Поиск",
  "act.git": "Система контроля версий",
  "act.runs": "Запуски агента",
  "act.settings": "Настройки",
  "panel.explorer": "Проводник",
  "panel.agent": "Агент",
  "panel.terminal": "Терминал",
  "panel.inside": "Внутри агента",
  "panel.output": "Вывод",
  "editor.empty.title": "Откройте файл, чтобы начать",
  "editor.empty.body": "Выберите файл в проводнике или попросите агента создать его.",
  "explorer.soon": "Дерево файлов появится на этапе 1.",
  "agent.empty.title": "Спросите агента",
  "agent.empty.body": "Опишите изменение обычными словами. Вы увидите каждый его шаг и проверите каждую правку до того, как она попадёт в код.",
  "agent.soon": "Чат появится на этапе 3.",
  "terminal.soon": "Настоящий терминал (в Docker-контейнере проекта) появится на этапе 2.",
  "inside.soon": "Визуализатор агента (схема, интерпретатор, вызовы инструментов) появится на этапе 4.",
  "status.connected": "Подключено",
  "status.connecting": "Подключение…",
  "status.offline": "Сервер недоступен",
  "status.locked": "Нет ключа сессии",
  "status.docker.ok": "Docker",
  "status.docker.off": "Docker остановлен",
  "status.docker.none": "Нет Docker",
  "status.git": "git",
  "theme.system": "Тема системы",
  "theme.dark": "Тёмная тема",
  "theme.light": "Светлая тема",
  "token.title": "Странице нужен ключ сессии",
  "token.body": "Ради безопасности Agent Smith Studio отвечает только по ссылке, которую печатает в терминале при запуске. Откройте её (в ней есть ?token=…).",
  "token.cmd": "Запуск студии",
  "offline.title": "Сервер студии недоступен",
  "offline.body": "agent-studio всё ещё запущен? Запустите его снова и обновите страницу.",
  "retry": "Повторить",
};

const fr: Dict = {
  "app.name": "Agent Smith Studio",
  "act.explorer": "Explorateur",
  "act.search": "Recherche",
  "act.git": "Contrôle de source",
  "act.runs": "Exécutions de l'agent",
  "act.settings": "Paramètres",
  "panel.explorer": "Explorateur",
  "panel.agent": "Agent",
  "panel.terminal": "Terminal",
  "panel.inside": "Dans l'agent",
  "panel.output": "Sortie",
  "editor.empty.title": "Ouvrez un fichier pour commencer",
  "editor.empty.body": "Choisissez un fichier dans l'explorateur ou demandez à l'agent d'en créer un.",
  "explorer.soon": "L'arborescence arrive à l'étape 1.",
  "agent.empty.title": "Demandez à l'agent",
  "agent.empty.body": "Décrivez une modification avec vos mots. Vous verrez chacune de ses étapes et relirez chaque modification avant qu'elle n'arrive dans le code.",
  "agent.soon": "Le chat arrive à l'étape 3.",
  "terminal.soon": "Un vrai terminal (dans le conteneur Docker du projet) arrive à l'étape 2.",
  "inside.soon": "Le visualiseur de l'agent (schéma, interpréteur, appels d'outils) arrive à l'étape 4.",
  "status.connected": "Connecté",
  "status.connecting": "Connexion…",
  "status.offline": "Serveur injoignable",
  "status.locked": "Pas de clé de session",
  "status.docker.ok": "Docker",
  "status.docker.off": "Docker arrêté",
  "status.docker.none": "Pas de Docker",
  "status.git": "git",
  "theme.system": "Thème du système",
  "theme.dark": "Thème sombre",
  "theme.light": "Thème clair",
  "token.title": "Cette page a besoin de sa clé de session",
  "token.body": "Par sécurité, Agent Smith Studio ne répond qu'au lien affiché dans le terminal au démarrage. Ouvrez ce lien (il contient ?token=…).",
  "token.cmd": "Démarrer le studio",
  "offline.title": "Impossible de joindre le serveur du studio",
  "offline.body": "agent-studio tourne-t-il toujours ? Relancez-le puis rechargez la page.",
  "retry": "Réessayer",
};

const DICTS: Record<Lang, Dict> = { en, ru, fr };

export function translate(lang: Lang, key: Key, params?: Record<string, string | number>): string {
  const s = DICTS[lang][key] ?? en[key];
  return params ? s.replace(/\{(\w+)\}/g, (m, k: string) => (params[k] != null ? String(params[k]) : m)) : s;
}

export const LangContext = createContext<Lang>("en");
export function useT(): (key: Key, params?: Record<string, string | number>) => string {
  const lang = useContext(LangContext);
  return (key, params) => translate(lang, key, params);
}
