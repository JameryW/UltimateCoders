import type { GrpcConnectionState } from "@/hooks/useGrpcWeb";

interface HeaderProps {
  connected: boolean;
  grpcState?: GrpcConnectionState;
  lastUpdate?: string;
}

const GRPC_LABELS: Record<GrpcConnectionState, { text: string; color: string }> = {
  connected: { text: "gRPC", color: "bg-blue-400" },
  connecting: { text: "gRPC…", color: "bg-yellow-400 animate-pulse" },
  disconnected: { text: "gRPC off", color: "bg-gray-500" },
  error: { text: "gRPC err", color: "bg-red-400" },
};

export function Header({ connected, grpcState, lastUpdate }: HeaderProps) {
  const grpc = grpcState ? GRPC_LABELS[grpcState] : null;
  return (
    <header className="border-b border-gray-700 px-6 py-4">
      <div className="max-w-7xl mx-auto flex items-center justify-between">
        <div className="flex items-center space-x-3">
          <h1 className="text-xl font-bold text-white">UltimateCoders</h1>
          <span className="text-sm text-gray-400">Dashboard</span>
        </div>
        <div className="flex items-center space-x-4">
          {lastUpdate && <span className="text-xs text-gray-500">{new Date(lastUpdate).toLocaleTimeString()}</span>}
          <span className={`pulse-dot ${connected ? "bg-green-500" : "bg-red-500"}`} title="SSE" />
          {grpc && (
            <span className="flex items-center space-x-1">
              <span className={`pulse-dot ${grpc.color}`} title="gRPC-Web" />
              <span className="text-xs text-gray-400">{grpc.text}</span>
            </span>
          )}
        </div>
      </div>
    </header>
  );
}
