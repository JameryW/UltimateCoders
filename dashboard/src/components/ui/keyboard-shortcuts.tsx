import { useEffect, useState, useCallback } from "react";

const SHORTCUTS = [
  { keys: "?", description: "Show keyboard shortcuts" },
  { keys: "Esc", description: "Close dialog / deselect task" },
  { keys: "T", description: "Scroll to Tasks" },
  { keys: "E", description: "Scroll to Event Log" },
  { keys: "W", description: "Toggle Workers panel" },
  { keys: "S", description: "Scroll to Search" },
  { keys: "D", description: "Toggle dark/light theme" },
];

export function KeyboardShortcuts() {
  const [open, setOpen] = useState(false);

  const handleKeyDown = useCallback((e: KeyboardEvent) => {
    // Ignore when typing in inputs
    if (e.target instanceof HTMLInputElement || e.target instanceof HTMLTextAreaElement) return;

    if (e.key === "?") {
      e.preventDefault();
      setOpen((v) => !v);
    }
    if (e.key === "Escape" && open) {
      setOpen(false);
    }

    // ponytail: single-key navigation shortcuts (only when no modal open)
    if (!open) {
      const scrollMap: Record<string, string> = {
        "t": "tasks",
        "e": "events",
        "s": "search",
        "f": "files",
      };
      const target = scrollMap[e.key.toLowerCase()];
      if (target) {
        e.preventDefault();
        document.getElementById(target)?.scrollIntoView({ behavior: "smooth", block: "start" });
      }
    }
  }, [open]);

  useEffect(() => {
    window.addEventListener("keydown", handleKeyDown);
    return () => window.removeEventListener("keydown", handleKeyDown);
  }, [handleKeyDown]);

  if (!open) return null;

  return (
    <div
      className="fixed inset-0 z-50 flex items-center justify-center bg-black/40"
      onClick={() => setOpen(false)}
      role="dialog"
      aria-modal="true"
      aria-label="Keyboard shortcuts"
    >
      <div
        className="bg-[var(--bg-surface)] border border-[var(--border-color)] rounded-xl p-6 max-w-sm w-[90%] shadow-xl"
        onClick={(e) => e.stopPropagation()}
      >
        <h2 className="text-base font-semibold text-[var(--text-primary)] mb-4">Keyboard Shortcuts</h2>
        <ul className="space-y-2">
          {SHORTCUTS.map((s) => (
            <li key={s.keys} className="flex items-center justify-between text-sm">
              <span className="text-[var(--text-secondary)]">{s.description}</span>
              <kbd className="bg-[var(--bg-primary)] border border-[var(--border-color)] rounded px-2 py-0.5 text-xs font-mono text-[var(--text-primary)]">
                {s.keys}
              </kbd>
            </li>
          ))}
        </ul>
        <p className="text-xs text-[var(--text-muted)] mt-4">Press <kbd className="bg-[var(--bg-primary)] border border-[var(--border-color)] rounded px-1.5 py-0.5 text-xs font-mono">?</kbd> or <kbd className="bg-[var(--bg-primary)] border border-[var(--border-color)] rounded px-1.5 py-0.5 text-xs font-mono">Esc</kbd> to close</p>
      </div>
    </div>
  );
}
