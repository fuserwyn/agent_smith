/* Per-viewer preferences (theme, language): localStorage, never required for the app to work. */
import { useCallback, useEffect, useState } from "react";

export function usePref<T extends string>(key: string, fallback: T, allowed: readonly T[]): [T, (v: T) => void] {
  const [value, setValue] = useState<T>(() => {
    try {
      const v = localStorage.getItem(key) as T | null;
      return v && allowed.includes(v) ? v : fallback;
    } catch {
      return fallback;
    }
  });
  const set = useCallback((v: T) => {
    setValue(v);
    try { localStorage.setItem(key, v); } catch { /* storage blocked */ }
  }, [key]);
  return [value, set];
}

export type ThemeChoice = "system" | "dark" | "light";
export const THEMES: readonly ThemeChoice[] = ["system", "dark", "light"];

/* Applies the theme to <html data-theme> ("system" follows the OS and reacts when it changes). */
export function useTheme(): [ThemeChoice, (t: ThemeChoice) => void, "dark" | "light"] {
  const [choice, setChoice] = usePref<ThemeChoice>("studio-theme", "system", THEMES);
  const query = "(prefers-color-scheme: light)";
  const [osLight, setOsLight] = useState(() => window.matchMedia(query).matches);
  useEffect(() => {
    const m = window.matchMedia(query);
    const on = () => setOsLight(m.matches);
    m.addEventListener("change", on);
    return () => m.removeEventListener("change", on);
  }, []);
  const resolved = choice === "system" ? (osLight ? "light" : "dark") : choice;
  useEffect(() => { document.documentElement.dataset.theme = resolved; }, [resolved]);
  return [choice, setChoice, resolved];
}
