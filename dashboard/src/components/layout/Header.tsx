interface HeaderProps {
  connected: boolean;
  lastUpdate?: string;
}

export function Header({ connected, lastUpdate }: HeaderProps) {
  return (
    <header className="border-b border-gray-700 px-6 py-4">
      <div className="max-w-7xl mx-auto flex items-center justify-between">
        <div className="flex items-center space-x-3">
          <h1 className="text-xl font-bold text-white">UltimateCoders</h1>
          <span className="text-sm text-gray-400">Dashboard</span>
        </div>
        <div className="flex items-center space-x-4">
          {lastUpdate && (
            <span className="text-xs text-gray-500">
              {new Date(lastUpdate).toLocaleTimeString()}
            </span>
          )}
          <span
            className={`pulse-dot ${connected ? "bg-green-500" : "bg-red-500"}`}
          />
        </div>
      </div>
    </header>
  );
}
