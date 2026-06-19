import type { GrpcConnectionState } from "@/hooks/useGrpcWeb";

interface ConnectionIndicatorProps {
  connected: boolean;
  grpcState?: GrpcConnectionState;
  onReconnectSSE?: () => void;
  onReconnectGrpc?: () => void;
}

export function ConnectionIndicator({ connected, grpcState, onReconnectSSE, onReconnectGrpc }: ConnectionIndicatorProps) {
  const grpcOk = grpcState === "connected";
  const grpcConnecting = grpcState === "connecting";
  const grpcReconnecting = grpcState === "reconnecting";
  const grpcError = grpcState === "error" || grpcState === "disconnected";

  // #11: Composite status — both channels work = "live", one down = "partial", both down = "offline"
  const anyConnected = connected || grpcOk;
  const bothConnected = connected && grpcOk;
  const compositeLabel = bothConnected ? "Live" : anyConnected ? "Partial" : "Offline";
  const compositeColor = bothConnected ? "bg-green-500/20 text-green-400 border-green-500/30"
    : anyConnected ? "bg-yellow-500/20 text-yellow-400 border-yellow-500/30"
    : "bg-red-500/20 text-red-400 border-red-500/30";

  return (
    <div className="fixed bottom-4 right-4 flex flex-col gap-1 z-50">
      {/* Composite status badge */}
      <div className={`px-3 py-1.5 rounded-md text-xs border ${compositeColor} font-medium`}>
        {compositeLabel}
      </div>
      {/* Per-channel detail (clickable for reconnect) */}
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
          className={`px-3 py-1.5 rounded-md text-xs cursor-pointer transition-colors duration-500 ${grpcOk ? "bg-blue-500/20 text-blue-400" : grpcConnecting || grpcReconnecting ? "bg-yellow-500/20 text-yellow-400" : "bg-red-500/20 text-red-400"}`}
          onClick={!grpcOk && onReconnectGrpc ? onReconnectGrpc : undefined}
          title={grpcReconnecting ? "gRPC reconnecting…" : grpcError ? "gRPC error — click to reconnect" : grpcOk ? "gRPC connected" : "gRPC connecting…"}
        >
          gRPC {grpcOk ? "●" : grpcConnecting || grpcReconnecting ? "◐" : "○"}
          {!grpcOk && onReconnectGrpc && " ↻"}
          {grpcReconnecting && " ⏳"}
        </div>
      )}
    </div>
  );
}
