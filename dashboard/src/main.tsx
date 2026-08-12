import { createRoot } from "react-dom/client";
import "./index.css";
import App from "./App.tsx";
import HomePage from "./pages/HomePage.tsx";
import TuiPage from "./pages/TuiPage.tsx";
import { ErrorBoundary } from "@/components/ui/ErrorBoundary";
import { initMermaid } from "./lib/mermaid.ts";
import { useEffect, useState } from "react";

// ponytail: apply stored theme synchronously before React renders, preventing FOUC.
try {
  const stored = localStorage.getItem("uc_dashboard_theme");
  if (stored === "light") {
    document.documentElement.setAttribute("data-theme", "light");
  } else if (stored === null && window.matchMedia("(prefers-color-scheme: light)").matches) {
    document.documentElement.setAttribute("data-theme", "light");
  }
} catch { /* ignore */ }

// The operations dashboard still renders Mermaid task graphs. Initialize its
// lazy renderer once at the application boundary so it remains available when
// the dashboard is opened after the overview or TUI route.
initMermaid();

// The product overview owns `/`; the legacy operations dashboard and terminal
// remain first-class routes. Keeping the routes hash-based preserves direct
// links from the dashboard's existing panel navigation.
// eslint-disable-next-line react-refresh/only-export-components -- entry file root component
function Root() {
  const [hash, setHash] = useState(() => window.location.hash);

  useEffect(() => {
    const handleHashChange = () => setHash(window.location.hash);
    window.addEventListener("hashchange", handleHashChange);
    return () => window.removeEventListener("hashchange", handleHashChange);
  }, []);

  if (hash.startsWith("#/tui")) return <TuiPage />;
  if (hash.startsWith("#/dashboard")) return <App />;
  return <HomePage />;
}

/** ponytail: last-resort root error boundary. Panel-level ErrorBoundaries
 * catches anything that escapes so the TUI shows a recoverable error screen
 * instead of a white screen. */
// eslint-disable-next-line react-refresh/only-export-components -- entry file with router and root boundary
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
  <ErrorBoundary fallbackRender={RootErrorFallback}>
    <Root />
  </ErrorBoundary>,
);
