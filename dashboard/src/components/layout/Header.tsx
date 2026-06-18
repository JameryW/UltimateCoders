import type { GrpcConnectionState } from "@/hooks/useGrpcWeb";
import type { Theme } from "@/hooks/useTheme";

interface HeaderProps {
  connected: boolean;
  grpcState?: GrpcConnectionState;
  lastUpdate?: string;
  theme: Theme;
  onToggleTheme: () => void;
  onLogout?: () => void;
}

const GRPC_LABELS: Record<GrpcConnectionState, { text: string; color: string }> = {
  connected: { text: "gRPC", color: "bg-blue-400" },
  connecting: { text: "gRPC...", color: "bg-yellow-400 animate-pulse" },
  disconnected: { text: "gRPC off", color: "bg-gray-500" },
  error: { text: "gRPC err", color: "bg-red-400" },
  exhausted: { text: "gRPC !!", color: "bg-orange-400" },
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
];

export function Header({ connected, grpcState, lastUpdate, theme, onToggleTheme, onLogout }: HeaderProps) {
  const grpc = grpcState ? GRPC_LABELS[grpcState] : null;
  return (
    <header className="border-b border-[var(--border-color)] px-6 py-3">
      <div className="max-w-7xl mx-auto flex items-center justify-between">
        <div className="flex items-center space-x-3">
          <h1 className="text-xl font-bold text-[var(--text-primary)]">UltimateCoders</h1>
          <span className="text-sm text-[var(--text-secondary)]">Dashboard</span>
        </div>
        {/* Nav links */}
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
        <div className="flex items-center space-x-4">
          {lastUpdate && <span className="text-xs text-[var(--text-muted)]">{new Date(lastUpdate).toLocaleTimeString()}</span>}
          <span className={`pulse-dot ${connected ? "bg-green-500" : "bg-red-500"}`} title="SSE" />
          {grpc && (
            <span className="flex items-center space-x-1">
              <span className={`pulse-dot ${grpc.color}`} title="gRPC-Web" />
              <span className="text-xs text-[var(--text-secondary)]">{grpc.text}</span>
            </span>
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
    </header>
  );
}
