/**
 * Worker Bridge — registers uc_worker tool with omp ExtensionAPI.
 *
 * Lets the LLM agent query worker status (online/offline, load,
 * capabilities, heartbeat) for smarter scheduling decisions, and
 * dynamically scale the worker cluster or force-deregister stale workers.
 */

import type { ExtensionAPI } from "@oh-my-pi/pi-coding-agent";
import { GrpcBridge } from "./grpc-bridge";

// ponytail: `as never` on parameters to dodge TS2589 deep instantiation
// in registerTool<TParams extends TSchema>. Runtime schema is correct.

export function registerWorkerTools(pi: ExtensionAPI, bridge: GrpcBridge): void {
	const workerSchema = pi.zod.object({
		action: pi.zod.enum(["list", "status", "scale", "deregister"]).describe(
			"Worker action: list all workers, check a specific worker's status, " +
			"scale the worker cluster to a target count, or force-deregister a stale worker"
		),
		// ponytail: worker_id supports prefix matching — e.g. "nats" matches "nats_worker"
		worker_id: pi.zod.string().optional().describe(
			"Worker ID (required for status and deregister; prefix matching supported for status)"
		),
		target_count: pi.zod.number().int().nonnegative().optional().describe(
			"Desired total worker count (required for scale action)"
		),
	});

	pi.registerTool({
		name: "uc_worker",
		label: "UC Worker",
		description:
			"Manage UltimateCoders workers. " +
			"List all connected workers with load/capabilities/heartbeat, " +
			"check a specific worker's availability (worker_id supports prefix matching), " +
			"scale the worker cluster to a target count (docker compose; workers self-register/deregister), " +
			"or force-deregister a stale/ghost worker from the registry. " +
			"Use list/status before submitting tasks to gauge cluster capacity.",
		parameters: workerSchema as never,
		async execute(_id, params: unknown, _signal, _onUpdate, _ctx) {
			const p = params as { action: string; worker_id?: string; target_count?: number };
			try {
				if (p.action === "scale") {
					if (p.target_count === undefined) {
						return {
							content: [{ type: "text" as const, text: "scale action requires target_count" }],
							useless: true,
						};
					}
					const target = p.target_count;
					const result = await bridge.scaleWorkers("scale", { targetCount: target });
					if (!result.success) {
						return {
							content: [{
								type: "text" as const,
								text: `Scale failed: ${result.error ?? result.message ?? "unknown error"}`,
							}],
							isError: true,
						};
					}
					return {
						content: [{
							type: "text" as const,
							text: `Scaled workers → target ${target} (${result.message}; actual_count=${result.actualCount}). ` +
								"Workers self-register on start and self-deregister on SIGTERM — use 'list' to verify.",
						}],
					};
				}

				if (p.action === "deregister") {
					if (!p.worker_id) {
						return {
							content: [{ type: "text" as const, text: "deregister action requires worker_id" }],
							useless: true,
						};
					}
					const wid = p.worker_id;
					const result = await bridge.scaleWorkers("deregister", { workerId: wid });
					if (!result.success) {
						return {
							content: [{
								type: "text" as const,
								text: `Deregister failed: ${result.error ?? result.message ?? "unknown error"}`,
							}],
							isError: true,
						};
					}
					return {
						content: [{
							type: "text" as const,
							text: `Deregistered worker ${wid} (actual_count=${result.actualCount}). ${result.message}`.trim(),
						}],
					};
				}

				const result = await bridge.listWorkers();

				if (!result.available) {
					return { content: [{ type: "text" as const, text: "(worker service unavailable — gRPC server may be down)" }] };
				}

				if (p.action === "status" && p.worker_id) {
					const wid = p.worker_id;
					// ponytail: F34 — a prefix matching multiple workers must not
					// silently pick the first (an LLM could schedule against the
					// wrong worker). Demand the full id when ambiguous.
					const matches = result.workers.filter((w) => w.id === wid || w.id.startsWith(wid));
					if (matches.length === 0) {
						return { content: [{ type: "text" as const, text: `Worker ${p.worker_id} not found` }], useless: true };
					}
					if (matches.length > 1) {
						return {
							content: [{
								type: "text" as const,
								text: `Worker prefix "${p.worker_id}" matches ${matches.length} workers: ${matches.map((m) => m.id).join(", ")} — use the full id`,
							}],
							isError: true,
						};
					}
					const w = matches[0];
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
