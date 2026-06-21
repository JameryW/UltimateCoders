import { useState } from "react";
import type { GrpcConnectionState } from "@/hooks/useGrpcWeb";
import type { DashboardConnectionState } from "@/hooks/useDashboardGrpc";
import type { Theme } from "@/hooks/useTheme";

interface HeaderProps {
  connected: boolean;
  grpcState?: GrpcConnectionState;
  grpcExhausted?: boolean;
  dashGrpcState?: DashboardConnectionState;
  lastUpdate?: string;
  theme: Theme;
  onToggleTheme: () => void;
  onLogout?: () => void;
  onReconnectGrpc?: () => void;
  onReconnectDashGrpc?: () => void;
  /** Panels that failed to load — show warning banner if non-empty. */
  fetchErrors?: Record<string, string>;
}

const GRPC_LABELS: Record<GrpcConnectionState, { text: string; color: string }> = {
  connected: { text: "gRPC", color: "bg-blue-400" },
  connecting: { text: "gRPC...", color: "bg-yellow-400 animate-pulse" },
  disconnected: { text: "gRPC off", color: "bg-gray-500" },
  error: { text: "gRPC err", color: "bg-red-400" },
  reconnecting: { text: "gRPC...", color: "bg-yellow-400 animate-pulse" },
};

const DASH_LABELS: Record<string, { text: string; color: string }> = {
  connected: { text: "Dash", color: "bg-green-400" },
  connecting: { text: "Dash...", color: "bg-yellow-400 animate-pulse" },
  disconnected: { text: "Dash off", color: "bg-gray-500" },
  error: { text: "Dash err", color: "bg-red-400" },
  reconnecting: { text: "Dash...", color: "bg-yellow-400 animate-pulse" },
};

const NAV_SECTIONS = [
  { hash: "tasks", label: "Tasks" },
  { hash: "events", label: "Events" },
  { hash: "workers", label: "Workers" },
  { hash: "health", label: "Health" },
  { hash: "circuit-breaker", label: "CB" },
  { hash: "scheduler", label: "Scheduler" },
  { hash: "chart", label: "Chart" },
  { hash: "search", label: "Search" },
  { hash: "files", label: "Files" },
];

export function Header({ connected, grpcState, grpcExhausted, dashGrpcState, lastUpdate, theme, onToggleTheme, onLogout, onReconnectGrpc, onReconnectDashGrpc, fetchErrors }: HeaderProps) {
  const [menuOpen, setMenuOpen] = useState(false);
  const grpc = grpcState ? GRPC_LABELS[grpcState] : null;
  const dash = dashGrpcState ? DASH_LABELS[dashGrpcState] : null;
  const grpcOk = grpcState === "connected";
  const dashOk = dashGrpcState === "connected";

  return (
    <header className="border-b border-[var(--border-color)] px-6 py-3">
      <div className="max-w-7xl mx-auto flex items-center justify-between">
        <div className="flex items-center space-x-3">
          <h1 className="text-xl font-bold text-[var(--text-primary)]">UltimateCoders</h1>
          <span className="text-sm text-[var(--text-secondary)]">Dashboard</span>
        </div>
        {/* Nav links — desktop */}
        <nav className="hidden md:flex items-center space-x-3">
          {NAV_SECTIONS.map((s) => (
            <a
              key={s.hash}
              href={`#${s.hash}`}
              className="text-xs text-[var(--text-muted)] hover:text-[var(--text-primary)] transition-colors"
            >
              {s.label}
            </a>
          ))}
        </nav>
        <div className="flex items-center space-x-3">
          {/* Hamburger — mobile only */}
          <button
            className="md:hidden p-1.5 rounded-md border border-[var(--border-color)] hover:bg-[var(--bg-surface-alt)] transition-colors"
            onClick={() => setMenuOpen((v) => !v)}
            aria-label="Toggle navigation menu"
          >
            {menuOpen ? (
              <svg xmlns="http://www.w3.org/2000/svg" className="h-4 w-4 text-[var(--text-secondary)]" fill="none" viewBox="0 0 24 24" stroke="currentColor" strokeWidth={2}>
                <path strokeLinecap="round" strokeLinejoin="round" d="M6 18L18 6M6 6l12 12" />
              </svg>
            ) : (
              <svg xmlns="http://www.w3.org/2000/svg" className="h-4 w-4 text-[var(--text-secondary)]" fill="none" viewBox="0 0 24 24" stroke="currentColor" strokeWidth={2}>
                <path strokeLinecap="round" strokeLinejoin="round" d="M4 6h16M4 12h16M4 18h16" />
              </svg>
            )}
          </button>
          {lastUpdate && <span className="text-xs text-[var(--text-muted)]">{new Date(lastUpdate).toLocaleTimeString()}</span>}
          <span className={`pulse-dot ${connected ? "bg-green-500" : "bg-red-500"}`} title={connected ? "Connected" : "Disconnected"} />
          {/* Task gRPC indicator — clickable to reconnect */}
          {grpc && (
            <button
              onClick={!grpcOk && !grpcExhausted && onReconnectGrpc ? onReconnectGrpc : undefined}
              title={grpcOk ? "Task gRPC connected" : grpcState === "reconnecting" ? "Task gRPC reconnecting..." : "Task gRPC error — click to reconnect"}
              className="flex items-center space-x-1 cursor-pointer"
            >
              <span className={`pulse-dot ${grpc.color}`} />
              <span className="text-xs text-[var(--text-secondary)]">{grpc.text}</span>
            </button>
          )}
          {/* Dashboard gRPC indicator — clickable to reconnect */}
          {dash && (
            <button
              onClick={!dashOk && onReconnectDashGrpc ? onReconnectDashGrpc : undefined}
              title={dashOk ? "Dashboard gRPC connected" : dashGrpcState === "reconnecting" ? "Dashboard gRPC reconnecting..." : "Dashboard gRPC error — click to reconnect"}
              className="flex items-center space-x-1 cursor-pointer"
            >
              <span className={`pulse-dot ${dash.color}`} />
              <span className="text-xs text-[var(--text-secondary)]">{dash.text}</span>
            </button>
          )}
          {grpcExhausted && grpcState === "reconnecting" && onReconnectGrpc && (
            <button
              onClick={onReconnectGrpc}
              className="text-xs text-red-400 hover:text-red-300"
              title="Task gRPC retries exhausted — click to retry"
            >
              ↻ retry
            </button>
          )}
          {/* Theme toggle */}
          <button
            onClick={onToggleTheme}
            title={`Switch to ${theme === "dark" ? "light" : "dark"} mode`}
            className="p-1.5 rounded-md border border-[var(--border-color)] hover:bg-[var(--bg-surface-alt)] transition-colors"
          >
            {theme === "dark" ? (
              <svg xmlns="http://www.w3.org/2000/svg" className="h-4 w-4 text-yellow-400" fill="none" viewBox="0 0 24 24" stroke="currentColor" strokeWidth={2}>
                <path strokeLinecap="round" strokeLinejoin="round" d="M12 3v1m0 16v1m9-9h-1M4 12H3m15.364 6.364l-.707-.707M6.343 6.343l-.707-.707m12.728 0l-.707.707M6.343 17.657l-.707.707M16 12a4 4 0 11-8 0 4 4 0 018 0z" />
              </svg>
            ) : (
              <svg xmlns="http://www.w3.org/2000/svg" className="h-4 w-4 text-indigo-600" fill="none" viewBox="0 0 24 24" stroke="currentColor" strokeWidth={2}>
                <path strokeLinecap="round" strokeLinejoin="round" d="M20.354 15.354A9 9 0 018.646 3.646 9.003 9.003 0 0012 21a9.003 9.003 0 008.354-5.646z" />
              </svg>
            )}
          </button>
          {/* Logout */}
          {onLogout && (
            <button
              onClick={onLogout}
              title="Logout"
              className="p-1.5 rounded-md border border-[var(--border-color)] hover:bg-[var(--bg-surface-alt)] transition-colors"
            >
              <svg xmlns="http://www.w3.org/2000/svg" className="h-4 w-4 text-[var(--text-secondary)]" fill="none" viewBox="0 0 24 24" stroke="currentColor" strokeWidth={2}>
                <path strokeLinecap="round" strokeLinejoin="round" d="M17 16l4-4m0 0l-4-4m4 4H7m6 4v1a3 3 0 01-3 3H6a3 3 0 01-3-3V7a3 3 0 013-3h4a3 3 0 013 3v1" />
              </svg>
            </button>
          )}
        </div>
      </div>
      {/* Partial failure warning banner */}
      {fetchErrors && Object.keys(fetchErrors).length > 0 && (
        <div className="mt-2 px-3 py-1.5 rounded-md text-xs bg-yellow-500/20 text-yellow-300 border border-yellow-500/40 flex items-center gap-2">
          <span>⚠</span>
          <span>Some panels unavailable: {Object.keys(fetchErrors).join(", ")}</span>
        </div>
      )}
      {/* Mobile nav dropdown */}
      {menuOpen && (
        <nav className="md:hidden mt-2 pb-1 flex flex-wrap gap-2">
          {NAV_SECTIONS.map((s) => (
            <a
              key={s.hash}
              href={`#${s.hash}`}
              onClick={() => setMenuOpen(false)}
              className="text-xs text-[var(--text-muted)] hover:text-[var(--text-primary)] transition-colors px-2 py-1 rounded hover:bg-[var(--bg-surface-alt)]"
            >
              {s.label}
            </a>
          ))}
        </nav>
      )}
    </header>
  );
}
