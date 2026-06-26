import { StrictMode, useState, useEffect } from "react";
import { createRoot } from "react-dom/client";
import "./index.css";
import App from "./App.tsx";
import TuiPage from "./pages/TuiPage.tsx";
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

createRoot(document.getElementById("root")!).render(
  <StrictMode>
    <Root />
  </StrictMode>,
);
