import { useState, useEffect, useCallback } from "react";

export type Theme = "dark" | "light";

const STORAGE_KEY = "uc_dashboard_theme";

/** Read persisted theme, falling back to system preference. */
function getStoredTheme(): Theme {
  try {
    const stored = localStorage.getItem(STORAGE_KEY);
    if (stored === "light" || stored === "dark") return stored;
  } catch { /* ignore */ }
  // ponytail: respect OS preference when no explicit choice stored
  return window.matchMedia("(prefers-color-scheme: light)").matches ? "light" : "dark";
}

/** Apply theme by setting the data-theme attribute on <html>. */
function applyTheme(theme: Theme) {
  document.documentElement.setAttribute("data-theme", theme);
}

/**
 * Theme hook — manages dark/light mode with localStorage persistence.
 *
 * On mount, reads the persisted preference (default: system preference)
 * and applies it via the `data-theme` attribute on the root <html> element.
 * CSS variables in index.css switch based on this attribute.
 */
export function useTheme() {
  const [theme, setThemeState] = useState<Theme>(getStoredTheme);

  // Apply on mount + listen for OS theme changes when no explicit choice stored
  useEffect(() => {
    applyTheme(theme);

    // ponytail: if user hasn't explicitly chosen, follow OS changes
    const hasExplicitChoice = localStorage.getItem(STORAGE_KEY) !== null;
    if (hasExplicitChoice) return;

    const mq = window.matchMedia("(prefers-color-scheme: light)");
    const onChange = (e: MediaQueryListEvent) => {
      const newTheme: Theme = e.matches ? "light" : "dark";
      setThemeState(newTheme);
      applyTheme(newTheme);
    };
    mq.addEventListener("change", onChange);
    return () => mq.removeEventListener("change", onChange);
  }, [theme]);

  const setTheme = useCallback((t: Theme) => {
    try {
      localStorage.setItem(STORAGE_KEY, t);
    } catch { /* ignore */ }
    setThemeState(t);
    applyTheme(t);
  }, []);

  const toggleTheme = useCallback(() => {
    setTheme(theme === "dark" ? "light" : "dark");
  }, [theme, setTheme]);

  return { theme, setTheme, toggleTheme };
}
