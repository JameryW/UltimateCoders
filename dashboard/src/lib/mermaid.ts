let initialized = false;

export function initMermaid() {
  if (initialized) return;
  if (typeof window === "undefined") return;

  import("mermaid").then((m) => {
    m.default.initialize({
      startOnLoad: false,
      theme: "dark",
      themeVariables: {
        primaryColor: "#1e293b",
        primaryBorderColor: "#334155",
        primaryTextColor: "#e2e8f0",
        lineColor: "#475569",
      },
      flowchart: { useMaxWidth: true, htmlLabels: true, curve: "basis" },
    });
    initialized = true;
  }).catch(() => {
    // Mermaid not available — graceful fallback
  });
}

export async function renderMermaid(
  id: string,
  definition: string,
): Promise<string | null> {
  try {
    const m = await import("mermaid");
    const result = await m.default.render(id, definition);
    return result.svg;
  } catch {
    return null;
  }
}
