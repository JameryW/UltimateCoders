import { createRoot } from "react-dom/client";
import "./index.css";
import { ErrorBoundary } from "@/components/ui/ErrorBoundary";
import { lazy, Suspense, useEffect, useState } from "react";

// Route-level code splitting: the product home and the operations dashboard
// are separate entry chunks, so `/` never downloads the dashboard bundle (and
// vice versa). Both keep the same hash/deep-link behaviour.
// eslint-disable-next-line react-refresh/only-export-components -- entry file with router
const App = lazy(() => import("./App.tsx"));
// eslint-disable-next-line react-refresh/only-export-components -- entry file with router
const HomePage = lazy(() => import("./pages/HomePage.tsx"));

// ponytail: apply stored theme synchronously before React renders, preventing FOUC.
try {
  const stored = localStorage.getItem("uc_dashboard_theme");
  if (stored === "light") {
    document.documentElement.setAttribute("data-theme", "light");
  } else if (stored === null && window.matchMedia("(prefers-color-scheme: light)").matches) {
    document.documentElement.setAttribute("data-theme", "light");
  }
} catch { /* ignore */ }

// The product overview owns `/`; the operations dashboard remains at
// `#/dashboard` for existing deep links.
// eslint-disable-next-line react-refresh/only-export-components -- entry file root component
function Root() {
  const [hash, setHash] = useState(() => window.location.hash);
  const isDashboardPath = window.location.pathname.replace(/\/+$/, "") === "/dashboard";

  useEffect(() => {
    const handleHashChange = () => setHash(window.location.hash);
    window.addEventListener("hashchange", handleHashChange);
    return () => window.removeEventListener("hashchange", handleHashChange);
  }, []);

  return (
    <Suspense fallback={<RouteSkeleton />}>
      {isDashboardPath || hash.startsWith("#/dashboard") ? <App /> : <HomePage />}
    </Suspense>
  );
}

/** Shimmer placeholder while the route chunk downloads (first paint stays branded). */
// eslint-disable-next-line react-refresh/only-export-components -- entry file with router
function RouteSkeleton() {
  return (
    <div className="route-skeleton" role="status" aria-live="polite" aria-label="Loading interface">
      <div className="route-skeleton-top" />
      <div className="route-skeleton-grid">
        {Array.from({ length: 6 }, (_, index) => <div key={index} className="route-skeleton-card" />)}
      </div>
    </div>
  );
}

/** Last-resort root error boundary for the overview and dashboard. */
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
