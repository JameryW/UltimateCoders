import { StrictMode, useState, useEffect } from "react";
import { createRoot } from "react-dom/client";
import "./index.css";
import App from "./App.tsx";
import TuiPage from "./pages/TuiPage.tsx";
import { ErrorBoundary } from "@/components/ui/ErrorBoundary";
import { initMermaid } from "./lib/mermaid.ts";

// ponytail: apply stored theme synchronously before React renders, preventing FOUC.
try {
  const stored = localStorage.getItem("uc_dashboard_theme");
  if (stored === "light") {
    document.documentElement.setAttribute("data-theme", "light");
  } else if (stored === null && window.matchMedia("(prefers-color-scheme: light)").matches) {
    document.documentElement.setAttribute("data-theme", "light");
  }
} catch { /* ignore */ }

initMermaid();

// ponytail: hash-based routing — #/tui gets TUI page, everything else gets dashboard
// eslint-disable-next-line react-refresh/only-export-components -- entry file with router
function Root() {
  const [hash, setHash] = useState(() => window.location.hash);
  useEffect(() => {
    const onHash = () => setHash(window.location.hash);
    window.addEventListener("hashchange", onHash);
    return () => window.removeEventListener("hashchange", onHash);
  }, []);

  if (hash === "#/tui") return <TuiPage />;
  return <App />;
}

/** ponytail: last-resort root error boundary. Panel-level ErrorBoundaries
 * isolate most crashes to a single panel; this catches anything that escapes
 * (an unwrapped component, a hook error) so the dashboard shows a recoverable
 * error screen + reload instead of a white screen. */
function RootErrorFallback(error: Error, retry: () => void) {
  return (
    <div className="min-h-screen flex items-center justify-center bg-[var(--bg-primary)] p-4" role="alert">
      <div className="max-w-md text-center">
        <p className="text-lg font-semibold text-red-500">Dashboard crashed</p>
        <p className="text-xs text-red-400 mt-2 font-mono break-all">{error.message}</p>
        <p className="text-sm text-[var(--text-muted)] mt-3">
          An unexpected error occurred. Reloading usually fixes it; retry re-renders without a full reload.
        </p>
        <div className="flex items-center justify-center gap-3 mt-4">
          <button
            onClick={retry}
            className="px-3 py-1.5 rounded-md border border-[var(--border-color)] text-sm text-[var(--text-primary)] hover:bg-[var(--bg-surface-alt)]"
          >
            Retry
          </button>
          <button
            onClick={() => window.location.reload()}
            className="px-3 py-1.5 rounded-md bg-blue-500 text-white text-sm hover:bg-blue-600"
          >
            Reload
          </button>
        </div>
      </div>
    </div>
  );
}

createRoot(document.getElementById("root")!).render(
  <StrictMode>
    <ErrorBoundary fallbackRender={RootErrorFallback}>
      <Root />
    </ErrorBoundary>
  </StrictMode>,
);
