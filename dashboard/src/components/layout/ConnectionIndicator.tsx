import type { GrpcConnectionState } from "@/hooks/useGrpcWeb";

interface ConnectionIndicatorProps {
  connected: boolean;
  grpcState?: GrpcConnectionState;
  onReconnectSSE?: () => void;
  onReconnectGrpc?: () => void;
}

export function ConnectionIndicator({ connected, grpcState, onReconnectSSE, onReconnectGrpc }: ConnectionIndicatorProps) {
  const grpcOk = grpcState === "connected";
  const grpcReconnecting = grpcState === "reconnecting";

  return (
    <div className="fixed bottom-4 right-4 flex flex-col gap-1 z-50">
      <div
        className={`px-3 py-1.5 rounded-md text-xs cursor-pointer transition-colors duration-500 ${connected ? "bg-green-500/20 text-green-400" : "bg-red-500/20 text-red-400"}`}
        onClick={!connected && onReconnectSSE ? onReconnectSSE : undefined}
        title={!connected ? "SSE disconnected — click to reconnect" : "SSE connected"}
      >
        SSE {connected ? "●" : "○"}
        {!connected && onReconnectSSE && " ↻"}
      </div>
      {grpcState && (
        <div
          className={`px-3 py-1.5 rounded-md text-xs cursor-pointer transition-colors duration-500 ${grpcOk ? "bg-blue-500/20 text-blue-400" : grpcState === "connecting" || grpcReconnecting ? "bg-yellow-500/20 text-yellow-400" : "bg-red-500/20 text-red-400"}`}
          onClick={!grpcOk && onReconnectGrpc ? onReconnectGrpc : undefined}
          title={grpcReconnecting ? "gRPC reconnecting…" : grpcState === "error" ? "gRPC error — click to reconnect" : grpcOk ? "gRPC connected" : "gRPC connecting…"}
        >
          gRPC {grpcOk ? "●" : grpcState === "connecting" || grpcReconnecting ? "◐" : "○"}
          {!grpcOk && onReconnectGrpc && " ↻"}
          {grpcReconnecting && " ⏳"}
        </div>
      )}
    </div>
  );
}
