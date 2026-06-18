import { StrictMode } from "react";
import { createRoot } from "react-dom/client";
import "./index.css";
import App from "./App.tsx";
import { initMermaid } from "./lib/mermaid.ts";

// ponytail: apply stored theme synchronously before React renders, preventing FOUC.
// The HTML default is data-theme="dark"; this overrides it if the user previously chose light.
try {
  const stored = localStorage.getItem("uc_dashboard_theme");
  if (stored === "light") document.documentElement.setAttribute("data-theme", "light");
} catch { /* ignore */ }

initMermaid();

createRoot(document.getElementById("root")!).render(
  <StrictMode>
    <App />
  </StrictMode>,
);
