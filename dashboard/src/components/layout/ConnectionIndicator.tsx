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
          className={`px-3 py-1.5 rounded-md text-xs cursor-pointer ${grpcOk ? "bg-blue-900 text-blue-300" : grpcState === "connecting" ? "bg-yellow-900 text-yellow-300" : "bg-red-900 text-red-300"}`}
          onClick={grpcError && onReconnectGrpc ? onReconnectGrpc : undefined}
          title={grpcError ? "gRPC error — click to reconnect" : grpcOk ? "gRPC connected" : "gRPC connecting…"}
        >
          gRPC {grpcOk ? "●" : grpcState === "connecting" ? "◐" : "○"}
          {grpcError && onReconnectGrpc && " ↻"}
        </div>
      )}
    </div>
  );
}
