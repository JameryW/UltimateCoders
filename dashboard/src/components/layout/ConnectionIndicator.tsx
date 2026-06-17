import type { GrpcConnectionState } from "@/hooks/useGrpcWeb";

interface ConnectionIndicatorProps {
  connected: boolean;
  grpcState?: GrpcConnectionState;
}

export function ConnectionIndicator({ connected, grpcState }: ConnectionIndicatorProps) {
  const grpcOk = grpcState === "connected";
  return (
    <div className="fixed bottom-4 right-4 flex flex-col gap-1 z-50">
      <div className={`px-3 py-1.5 rounded-md text-xs ${connected ? "bg-green-900 text-green-300" : "bg-red-900 text-red-300"}`}>
        SSE {connected ? "●" : "○"}
      </div>
      {grpcState && (
        <div className={`px-3 py-1.5 rounded-md text-xs ${grpcOk ? "bg-blue-900 text-blue-300" : grpcState === "connecting" ? "bg-yellow-900 text-yellow-300" : "bg-red-900 text-red-300"}`}>
          gRPC {grpcOk ? "●" : grpcState === "connecting" ? "◐" : "○"}
        </div>
      )}
    </div>
  );
}
