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
        className={`px-3 py-1.5 rounded-md text-xs cursor-pointer ${connected ? "bg-green-900 text-green-300" : "bg-red-900 text-red-300"}`}
        onClick={!connected && onReconnectSSE ? onReconnectSSE : undefined}
        title={!connected ? "SSE disconnected — click to reconnect" : "SSE connected"}
      >
        SSE {connected ? "●" : "○"}
        {!connected && onReconnectSSE && " ↻"}
      </div>
      {grpcState && (
        <div
          className={`px-3 py-1.5 rounded-md text-xs cursor-pointer ${grpcOk ? "bg-blue-900 text-blue-300" : grpcState === "connecting" ? "bg-yellow-900 text-yellow-300" : grpcExhausted ? "bg-orange-900 text-orange-300" : "bg-red-900 text-red-300"}`}
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
        <div className="px-3 py-2 rounded-md text-xs bg-orange-900/80 text-orange-200 border border-orange-600">
          <p className="font-medium mb-1">gRPC connection lost</p>
          <p className="text-orange-300">Auto-retry exhausted. Click gRPC ↻ above or check your server.</p>
        </div>
      )}
      {/* Hint for gRPC error (not yet exhausted) */}
      {grpcError && !grpcExhausted && (
        <div className="px-3 py-2 rounded-md text-xs bg-red-900/80 text-red-200 border border-red-600 max-w-56">
          <p className="text-red-300">gRPC server unreachable — is the Rust server running?</p>
        </div>
      )}
    </div>
  );
}
