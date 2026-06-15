interface ConnectionIndicatorProps {
  connected: boolean;
}

export function ConnectionIndicator({ connected }: ConnectionIndicatorProps) {
  return (
    <div
      className={`fixed bottom-4 right-4 px-3 py-1.5 rounded-md text-xs z-50 ${
        connected
          ? "bg-green-900 text-green-300"
          : "bg-red-900 text-red-300"
      }`}
    >
      {connected ? "Connected" : "Disconnected"}
    </div>
  );
}
