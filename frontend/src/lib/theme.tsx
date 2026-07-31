// Theme state. The class is applied to <html> before first paint by the inline
// script in index.html; this only keeps React in sync and handles changes.

import { createContext, useCallback, useContext, useEffect, useState, type ReactNode } from "react";

export type Theme = "light" | "dark" | "system";

const STORAGE_KEY = "cl-theme";

type ThemeState = {
  theme: Theme;
  /** What is actually on screen — `system` resolved against the media query. */
  resolved: "light" | "dark";
  setTheme: (t: Theme) => void;
};

const ThemeCtx = createContext<ThemeState | null>(null);

function stored(): Theme {
  try {
    const v = localStorage.getItem(STORAGE_KEY);
    if (v === "light" || v === "dark" || v === "system") return v;
  } catch {
    // Private mode or storage disabled — fall through to the system default.
  }
  return "system";
}

function systemPrefersDark(): boolean {
  return typeof matchMedia === "function" && matchMedia("(prefers-color-scheme: dark)").matches;
}

function apply(theme: Theme): "light" | "dark" {
  const dark = theme === "dark" || (theme === "system" && systemPrefersDark());
  document.documentElement.classList.toggle("dark", dark);
  return dark ? "dark" : "light";
}

export function ThemeProvider({ children }: { children: ReactNode }) {
  const [theme, setThemeState] = useState<Theme>(stored);
  const [resolved, setResolved] = useState<"light" | "dark">(() =>
    theme === "dark" || (theme === "system" && systemPrefersDark()) ? "dark" : "light",
  );

  useEffect(() => {
    setResolved(apply(theme));
  }, [theme]);

  // Only follow the OS while the user has not made an explicit choice.
  useEffect(() => {
    if (theme !== "system" || typeof matchMedia !== "function") return;
    const mq = matchMedia("(prefers-color-scheme: dark)");
    const onChange = () => setResolved(apply("system"));
    mq.addEventListener("change", onChange);
    return () => mq.removeEventListener("change", onChange);
  }, [theme]);

  const setTheme = useCallback((t: Theme) => {
    setThemeState(t);
    try {
      localStorage.setItem(STORAGE_KEY, t);
    } catch {
      // Not persisting is survivable; the theme still applies for this session.
    }
  }, []);

  return <ThemeCtx.Provider value={{ theme, resolved, setTheme }}>{children}</ThemeCtx.Provider>;
}

export function useTheme() {
  const v = useContext(ThemeCtx);
  if (!v) throw new Error("useTheme must be inside <ThemeProvider>");
  return v;
}
