import { useEffect, useMemo, useRef, useState } from "react";

/** One runnable palette entry. `keywords` widen the match without being shown. */
export type PaletteCommand = {
  id: string;
  label: string;
  hint?: string;
  keywords?: string;
  run: () => void;
};

/**
 * Case-insensitive substring filter over label/hint/keywords.
 * Empty query keeps the original order so callers control prioritisation.
 */
// eslint-disable-next-line react-refresh/only-export-components -- pure helper lives beside its palette
export function filterPaletteCommands(commands: PaletteCommand[], query: string): PaletteCommand[] {
  const needle = query.trim().toLowerCase();
  if (!needle) return commands;
  return commands.filter((command) => {
    const haystack = `${command.label} ${command.hint ?? ""} ${command.keywords ?? ""}`.toLowerCase();
    return haystack.includes(needle);
  });
}

/**
 * Ctrl+K command palette: a keyboard-first launcher for views and dashboard
 * actions. Arrow keys move, Enter runs, Escape closes; clicking the backdrop
 * also closes. Rendering is skipped entirely while closed, and the dialog is
 * remounted on each open so its query/selection state starts clean.
 */
export function CommandPalette({
  open,
  commands,
  onClose,
}: {
  open: boolean;
  commands: PaletteCommand[];
  onClose: () => void;
}) {
  if (!open) return null;
  return <PaletteDialog commands={commands} onClose={onClose} />;
}

function PaletteDialog({ commands, onClose }: { commands: PaletteCommand[]; onClose: () => void }) {
  const [query, setQuery] = useState("");
  const [activeIndex, setActiveIndex] = useState(0);
  const inputRef = useRef<HTMLInputElement>(null);

  const filtered = useMemo(() => filterPaletteCommands(commands, query), [commands, query]);
  const safeIndex = Math.min(activeIndex, Math.max(filtered.length - 1, 0));

  // Focus the search field once the dialog is in the DOM (external system).
  useEffect(() => {
    const timer = window.setTimeout(() => inputRef.current?.focus(), 0);
    return () => window.clearTimeout(timer);
  }, []);

  useEffect(() => {
    const onKeyDown = (event: KeyboardEvent) => {
      if (event.key === "Escape") {
        event.preventDefault();
        onClose();
        return;
      }
      if (event.key === "ArrowDown") {
        event.preventDefault();
        setActiveIndex((index) => Math.min(index + 1, Math.max(filtered.length - 1, 0)));
        return;
      }
      if (event.key === "ArrowUp") {
        event.preventDefault();
        setActiveIndex((index) => Math.max(index - 1, 0));
        return;
      }
      if (event.key === "Enter") {
        const command = filtered[Math.min(activeIndex, Math.max(filtered.length - 1, 0))];
        if (!command) return;
        event.preventDefault();
        command.run();
        onClose();
      }
    };
    window.addEventListener("keydown", onKeyDown);
    return () => window.removeEventListener("keydown", onKeyDown);
  }, [filtered, activeIndex, onClose]);

  return (
    <div className="terminal-palette-backdrop" role="presentation" onClick={onClose}>
      <div
        className="terminal-palette"
        role="dialog"
        aria-modal="true"
        aria-label="Command palette"
        onClick={(event) => event.stopPropagation()}
      >
        <input
          ref={inputRef}
          className="terminal-palette-input"
          value={query}
          onChange={(event) => {
            setQuery(event.target.value);
            setActiveIndex(0);
          }}
          placeholder="命令 / 视图 / 操作…"
          aria-label="Command palette search"
          autoComplete="off"
        />
        {filtered.length === 0 ? (
          <div className="terminal-palette-empty">无匹配命令</div>
        ) : (
          <ul className="terminal-palette-list" role="listbox" aria-label="Commands">
            {filtered.map((command, index) => (
              <li key={command.id} role="option" aria-selected={index === safeIndex}>
                <button
                  type="button"
                  className={`terminal-palette-item${index === safeIndex ? " is-active" : ""}`}
                  onMouseEnter={() => setActiveIndex(index)}
                  onClick={() => {
                    command.run();
                    onClose();
                  }}
                >
                  <span>{command.label}</span>
                  {command.hint ? <small>{command.hint}</small> : null}
                </button>
              </li>
            ))}
          </ul>
        )}
        <div className="terminal-palette-foot">↑↓ 选择 · ⏎ 执行 · Esc 关闭</div>
      </div>
    </div>
  );
}
