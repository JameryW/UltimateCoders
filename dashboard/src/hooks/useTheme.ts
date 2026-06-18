import { useState, useEffect, useCallback } from "react";

export type Theme = "dark" | "light";

const STORAGE_KEY = "uc_dashboard_theme";

/** Read persisted theme, defaulting to "dark". */
function getStoredTheme(): Theme {
  try {
    const stored = localStorage.getItem(STORAGE_KEY);
    if (stored === "light" || stored === "dark") return stored;
  } catch { /* ignore */ }
  return "dark";
}

/** Apply theme by setting the data-theme attribute on <html>. */
function applyTheme(theme: Theme) {
  document.documentElement.setAttribute("data-theme", theme);
}

/**
 * Theme hook — manages dark/light mode with localStorage persistence.
 *
 * On mount, reads the persisted preference (default: dark) and applies
 * it via the `data-theme` attribute on the root <html> element.
 * CSS variables in index.css switch based on this attribute.
 */
export function useTheme() {
  const [theme, setThemeState] = useState<Theme>(getStoredTheme);

  // Apply on mount
  useEffect(() => {
    applyTheme(theme);
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
