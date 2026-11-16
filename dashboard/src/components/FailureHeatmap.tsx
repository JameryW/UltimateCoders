import type {
  FailureHeatmap as FailureHeatmapData,
  FailureHeatmapCell,
  FailurePhase,
} from "@/lib/failureHeatmap";
import "../styles/failureHeatmap.css";

export interface FailureHeatmapProps {
  data: FailureHeatmapData;
}

const HEAT_LEGEND = [
  { className: "heat-0", label: "none" },
  { className: "heat-1", label: "low" },
  { className: "heat-2", label: "moderate" },
  { className: "heat-3", label: "high" },
  { className: "heat-4", label: "critical" },
] as const;

function cellKey(phase: FailurePhase, modeId: string): string {
  return `${phase}:${modeId}`;
}

function heatClass(count: number, maxCount: number): string {
  if (count <= 0 || maxCount <= 0) return "heat-0";
  const share = count / maxCount;
  if (share >= 0.75) return "heat-4";
  if (share >= 0.5) return "heat-3";
  if (share >= 0.25) return "heat-2";
  return "heat-1";
}

function formatRate(rate: number): string {
  const bounded = Number.isFinite(rate) ? Math.min(1, Math.max(0, rate)) : 0;
  return `${(bounded * 100).toFixed(1)}%`;
}

function phaseLabel(phase: FailurePhase): string {
  return phase.charAt(0).toUpperCase() + phase.slice(1);
}

/**
 * Pure presentational failure-mode heatmap: one row per mode, one column per
 * process phase. Cells use the `heat-0` … `heat-4` classes, with a legend and a
 * compact totals/deterministic-rate summary. No fetching or effects.
 */
export function FailureHeatmap({ data }: FailureHeatmapProps) {
  const cellsByKey = new Map<string, FailureHeatmapCell>();
  let maxCount = 0;
  for (const cell of data.cells) {
    cellsByKey.set(cellKey(cell.phase, cell.modeId), cell);
    if (cell.count > maxCount) maxCount = cell.count;
  }
  const deterministicModes = data.modes.filter((mode) => mode.deterministic).length;
  const summary =
    data.totalEvents === 0
      ? "No failure events recorded in this window."
      : `${data.totalEvents} failure ${data.totalEvents === 1 ? "event" : "events"} · ${formatRate(data.deterministicRate)} deterministic`;

  return (
    <section className="failure-heatmap" aria-label="Failure-mode heatmap">
      <div className="failure-heatmap__summary">
        <span>{summary}</span>
        <span>
          {deterministicModes}/{data.modes.length} modes deterministic
        </span>
      </div>

      <div className="failure-heatmap__scroller">
        <table className="failure-heatmap__table">
          <caption>Failure modes by process phase</caption>
          <thead>
            <tr>
              <th scope="col" className="failure-heatmap__mode-heading">
                Failure mode
              </th>
              {data.phases.map((phase) => (
                <th key={phase} scope="col" className="failure-heatmap__phase-heading">
                  {phaseLabel(phase)}
                </th>
              ))}
            </tr>
          </thead>
          <tbody>
            {data.modes.map((mode) => (
              <tr key={mode.id}>
                <th scope="row" className="failure-heatmap__mode">
                  <span>{mode.label}</span>
                  <span className="failure-heatmap__severity" title={`Severity ${mode.severity} of 5`}>
                    S{mode.severity}
                  </span>
                </th>
                {data.phases.map((phase) => {
                  const cell = cellsByKey.get(cellKey(phase, mode.id));
                  const count = cell?.count ?? 0;
                  const rate = cell?.rate ?? 0;
                  const deterministic = cell?.deterministic ?? mode.deterministic;
                  return (
                    <td
                      key={phase}
                      className={`failure-heatmap__cell ${heatClass(count, maxCount)}`}
                      title={`${mode.label} · ${phaseLabel(phase)} · ${count} ${count === 1 ? "event" : "events"} (${formatRate(rate)})${deterministic ? " · deterministic" : ""}`}
                      aria-label={`${mode.label}, ${phaseLabel(phase)}: ${count} ${count === 1 ? "event" : "events"}, ${formatRate(rate)}`}
                    >
                      {count > 0 ? count : ""}
                    </td>
                  );
                })}
              </tr>
            ))}
          </tbody>
        </table>
      </div>

      <div className="failure-heatmap__legend" aria-label="Heat legend">
        {HEAT_LEGEND.map((item) => (
          <span key={item.className} className="failure-heatmap__legend-item">
            <span className={`failure-heatmap__legend-swatch ${item.className}`} aria-hidden="true" />
            {item.label}
          </span>
        ))}
      </div>
    </section>
  );
}
