import { Card, CardHeader, CardTitle } from "@/components/ui/card";
import { Badge } from "@/components/ui/badge";
import { cn, shortId } from "@/lib/utils";
import type { WorkersData } from "@/types/dashboard";

function loadBarColor(percent: number): string {
  if (percent >= 100) return "bg-red-500";
  if (percent >= 75) return "bg-yellow-500";
  return "bg-green-500";
}

export function WorkersPanel({ data, stale }: { data: WorkersData; stale?: boolean }) {
  return (
    <Card stale={stale}>
      <CardHeader>
        <CardTitle>Workers</CardTitle>
        <Badge variant="ok">
          {data.available_count}/{data.total}
        </Badge>
      </CardHeader>

      {!data.available ? (
        <p className="text-sm text-gray-500">Workers not available</p>
      ) : data.workers.length === 0 ? (
        <p className="text-sm text-gray-500">No workers connected</p>
      ) : (
        <ul className="space-y-2">
          {data.workers.map((w) => (
            <li
              key={w.id}
              className={cn(
                "border-l-2 pl-2 py-1",
                w.is_available ? "border-l-green-500" : "border-l-red-500"
              )}
            >
              <div className="flex items-center justify-between text-sm">
                <span className="font-mono text-gray-300">{shortId(w.id)}</span>
                {w.heartbeat_stale && (
                  <span className="text-yellow-500 text-xs" title="Heartbeat stale">
                    &#9888;
                  </span>
                )}
              </div>

              <div className="mt-1">
                <div className="load-bar w-full">
                  <div
                    className={cn("load-bar-fill", loadBarColor(w.load_percent))}
                    style={{ width: `${Math.min(w.load_percent, 100)}%` }}
                  />
                </div>
                <div className="flex justify-between text-xs text-gray-500 mt-0.5">
                  <span>
                    {w.current_load}/{w.max_capacity}
                  </span>
                  <span>{w.load_percent}%</span>
                </div>
              </div>

              {w.capabilities.length > 0 && (
                <div className="flex flex-wrap gap-1 mt-1">
                  {w.capabilities.map((cap) => (
                    <span
                      key={cap}
                      className="text-xs bg-dark-700 text-gray-400 px-1.5 py-0.5 rounded"
                    >
                      {cap}
                    </span>
                  ))}
                </div>
              )}
            </li>
          ))}
        </ul>
      )}
    </Card>
  );
}
