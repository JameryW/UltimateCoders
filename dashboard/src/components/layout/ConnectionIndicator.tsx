import type { GrpcConnectionState } from "@/hooks/useGrpcWeb";
import type { DashboardConnectionState } from "@/hooks/useDashboardGrpc";

interface ConnectionIndicatorProps {
  connected?: boolean; // kept for backward compatibility but ignored
  grpcState?: GrpcConnectionState;
  grpcError?: boolean;
  grpcExhausted?: boolean;
  dashGrpcState?: DashboardConnectionState;
  onReconnectSSE?: () => void; // deprecated, kept for type compat
  onReconnectGrpc?: () => void;
  onDisconnectGrpc?: () => void;
  onReconnectDashGrpc?: () => void;
  onDisconnectDashGrpc?: () => void;
}

export function ConnectionIndicator({ grpcState, grpcError, grpcExhausted, dashGrpcState, onReconnectGrpc, onDisconnectGrpc, onReconnectDashGrpc, onDisconnectDashGrpc }: ConnectionIndicatorProps) {
  const grpcOk = grpcState === "connected";
  const grpcReconnecting = grpcState === "reconnecting";
  const dashOk = dashGrpcState === "connected";
  const dashReconnecting = dashGrpcState === "reconnecting";

  return (
    <div className="fixed bottom-4 right-4 flex flex-col gap-1 z-50">
      {/* Task gRPC (WatchTask stream) */}
      {grpcState && (
        <div
          className={`px-3 py-1.5 rounded-md text-xs cursor-pointer transition-colors duration-500 ${grpcOk ? "bg-blue-500/20 text-blue-400" : grpcState === "connecting" || grpcReconnecting ? "bg-yellow-500/20 text-yellow-400" : "bg-red-500/20 text-red-400"}`}
          onClick={!grpcOk && !grpcExhausted && onReconnectGrpc ? onReconnectGrpc : undefined}
          title={grpcReconnecting ? "Task gRPC reconnecting..." : grpcState === "error" ? "Task gRPC error -- click to reconnect" : grpcOk ? "Task gRPC connected" : "Task gRPC connecting..."}
        >
          Task gRPC {grpcOk ? "●" : grpcState === "connecting" || grpcReconnecting ? "◐" : "○"}
          {!grpcOk && !grpcExhausted && onReconnectGrpc && " ↻"}
          {grpcReconnecting && " ⏳"}
        </div>
      )}
      {/* Dashboard gRPC (WatchDashboard stream) */}
      {dashGrpcState && (
        <div
          className={`px-3 py-1.5 rounded-md text-xs cursor-pointer transition-colors duration-500 ${dashOk ? "bg-green-500/20 text-green-400" : dashGrpcState === "connecting" || dashReconnecting ? "bg-yellow-500/20 text-yellow-400" : "bg-red-500/20 text-red-400"}`}
          onClick={!dashOk && onReconnectDashGrpc ? onReconnectDashGrpc : undefined}
          title={dashReconnecting ? "Dashboard gRPC reconnecting..." : dashGrpcState === "error" ? "Dashboard gRPC error -- click to reconnect" : dashOk ? "Dashboard gRPC connected" : "Dashboard gRPC connecting..."}
        >
          Dash gRPC {dashOk ? "●" : dashGrpcState === "connecting" || dashReconnecting ? "◐" : "○"}
          {!dashOk && onReconnectDashGrpc && " ↻"}
          {dashReconnecting && " ⏳"}
        </div>
      )}
      {/* Stop reconnecting button when Task gRPC retries are exhausted */}
      {grpcExhausted && grpcReconnecting && onDisconnectGrpc && (
        <button
          onClick={onDisconnectGrpc}
          className="px-3 py-2 rounded-md text-xs bg-red-900/80 text-red-200 border border-red-600 hover:bg-red-800/80 transition-colors cursor-pointer"
          title="Stop Task gRPC reconnection attempts"
        >
          Stop reconnecting
        </button>
      )}
      {/* Hint for gRPC error (not yet exhausted) */}
      {grpcError && !grpcExhausted && (
        <div className="px-3 py-2 rounded-md text-xs bg-red-900/80 text-red-200 border border-red-600 max-w-56">
          <p className="text-red-300">gRPC server unreachable -- is the Rust server running?</p>
        </div>
      )}
    </div>
  );
}
