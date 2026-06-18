import type { GrpcConnectionState } from "@/hooks/useGrpcWeb";

interface ConnectionIndicatorProps {
  connected: boolean;
  grpcState?: GrpcConnectionState;
  onReconnectSSE?: () => void;
  onReconnectGrpc?: () => void;
}

export function ConnectionIndicator({ connected, grpcState, onReconnectSSE, onReconnectGrpc }: ConnectionIndicatorProps) {
  const grpcOk = grpcState === "connected";
  const grpcError = grpcState === "error";
  const grpcExhausted = grpcState === "exhausted";

  return (
    <div className="fixed bottom-4 right-4 flex flex-col gap-1 z-50">
      <div
        className={`px-3 py-1.5 rounded-md text-xs cursor-pointer ${connected ? "bg-green-500/20 text-green-400" : "bg-red-500/20 text-red-400"}`}
        onClick={!connected && onReconnectSSE ? onReconnectSSE : undefined}
        title={!connected ? "SSE disconnected — click to reconnect" : "SSE connected"}
      >
        SSE {connected ? "●" : "○"}
        {!connected && onReconnectSSE && " ↻"}
      </div>
      {grpcState && (
        <div
          className={`px-3 py-1.5 rounded-md text-xs cursor-pointer ${grpcOk ? "bg-blue-500/20 text-blue-400" : grpcState === "connecting" ? "bg-yellow-500/20 text-yellow-400" : grpcExhausted ? "bg-orange-500/20 text-orange-400" : "bg-red-500/20 text-red-400"}`}
          onClick={!grpcOk && onReconnectGrpc ? onReconnectGrpc : undefined}
          title={grpcExhausted ? "gRPC retry exhausted — click to reconnect" : grpcError ? "gRPC error — click to reconnect" : grpcOk ? "gRPC connected" : "gRPC connecting…"}
        >
          gRPC {grpcOk ? "●" : grpcState === "connecting" ? "◐" : "○"}
          {!grpcOk && onReconnectGrpc && " ↻"}
          {grpcExhausted && " ⚠"}
        </div>
      )}
      {/* Persistent banner when gRPC retries exhausted */}
      {grpcExhausted && (
        <div className="px-3 py-2 rounded-md text-xs bg-orange-500/20 text-orange-300 border border-orange-500/40">
          <p className="font-medium mb-1">gRPC connection lost</p>
          <p className="text-orange-400">Auto-retry exhausted. Click gRPC ↻ above or check your server.</p>
        </div>
      )}
    </div>
  );
}
