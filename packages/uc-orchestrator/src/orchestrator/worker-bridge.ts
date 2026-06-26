/**
 * Worker Bridge — registers uc_worker tool with omp ExtensionAPI.
 *
 * Lets the LLM agent query worker status (online/offline, load,
 * capabilities, heartbeat freshness) for smarter scheduling decisions.
 */

import type { ExtensionAPI } from "@oh-my-pi/pi-coding-agent";
import { GrpcBridge } from "./grpc-bridge";

// ponytail: `as never` on parameters to dodge TS2589 deep instantiation
// in registerTool<TParams extends TSchema>. Runtime schema is correct.

export function registerWorkerTools(pi: ExtensionAPI, bridge: GrpcBridge): void {
	const workerSchema = pi.zod.object({
		action: pi.zod.enum(["list", "status"]).describe("Worker action: list all workers, or check a specific worker's status"),
		// ponytail: worker_id supports prefix matching — e.g. "local" matches "local_worker"
		worker_id: pi.zod.string().optional().describe("Worker ID (required for status action; prefix matching supported)"),
	});

	pi.registerTool({
		name: "uc_worker",
		label: "UC Worker",
		description:
			"Query UltimateCoders worker status. " +
			"List all connected workers with load/capabilities/heartbeat, " +
			"or check a specific worker's availability (worker_id supports prefix matching). " +
			"Use this before submitting tasks to gauge cluster capacity.",
		parameters: workerSchema as never,
		async execute(_id, params: unknown, _signal, _onUpdate, _ctx) {
			const p = params as { action: string; worker_id?: string };
			try {
				const result = await bridge.listWorkers();

				if (!result.available) {
					return { content: [{ type: "text" as const, text: "(worker service unavailable — gRPC server may be down)" }] };
				}

				if (p.action === "status" && p.worker_id) {
					const wid = p.worker_id;
					const w = result.workers.find((w) => w.id === wid || w.id.startsWith(wid));
					if (!w) {
						return { content: [{ type: "text" as const, text: `Worker ${p.worker_id} not found` }], useless: true };
					}
					const age = w.heartbeatAgeSeconds < 60
						? `${Math.round(w.heartbeatAgeSeconds)}s ago`
						: `${Math.floor(w.heartbeatAgeSeconds / 60)}m ago`;
					const loadLine = w.maxCapacity < 0
						? `  Load: unknown (degraded mode)`
						: `  Load: ${w.currentLoad}/${w.maxCapacity} (${w.loadPercent}%)`;
					return {
						content: [{
							type: "text" as const,
							text: [
								`Worker ${w.id}${result.degraded ? " (degraded)" : ""}`,
								`  Status: ${w.isAvailable ? "online" : "offline"}${w.heartbeatStale ? " (stale heartbeat)" : ""}`,
								loadLine,
								`  Heartbeat: ${age}`,
								`  Capabilities: ${w.capabilities.length > 0 ? w.capabilities.join(", ") : "(none)"}`,
							].join("\n"),
						}],
					};
				}

				// list action
				if (result.workers.length === 0) {
					return { content: [{ type: "text" as const, text: "(no workers connected)" }], useless: true };
				}
				const lines = result.workers.map((w) => {
					const icon = w.isAvailable ? "ok" : "off";
					const stale = w.heartbeatStale ? " stale" : "";
					const caps = w.capabilities.length > 0 ? ` [${w.capabilities.join(",")}]` : "";
					const load = w.maxCapacity < 0
						? "unknown"
						: `${w.currentLoad}/${w.maxCapacity} (${w.loadPercent}%)`;
					return `${icon} ${w.id.slice(0, 12)}: ${load}${stale}${caps}`;
				});
				const degradedTag = result.degraded ? " (degraded)" : "";
				const summary = `Workers: ${result.availableCount}/${result.total} available${degradedTag}`;
				return { content: [{ type: "text" as const, text: `${summary}\n${lines.join("\n")}` }] };
			} catch (err) {
				return {
					content: [{
						type: "text" as const,
						text: `UC Worker error: ${err instanceof Error ? err.message : String(err)}`,
					}],
					isError: true,
				};
			}
		},
	});
}
