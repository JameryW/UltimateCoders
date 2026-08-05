/**
 * Scheduler Bridge — registers uc_scheduler_* tools with omp ExtensionAPI.
 *
 * Routes scheduler operations through GrpcBridge to the UC Rust engine's
 * DashboardService (GetSchedulerStatus, TriggerSchedulerJob, AddCronJob,
 * RemoveJob). Lets the LLM agent inspect the cron scheduler, trigger jobs
 * on demand, add new cron jobs, and remove jobs.
 *
 * Mirrors memory-bridge.ts / worker-bridge.ts pattern.
 */

import type { ExtensionAPI } from "@oh-my-pi/pi-coding-agent";
import { GrpcBridge } from "./grpc-bridge";

// ponytail: `as never` on parameters to dodge TS2589 deep instantiation
// in registerTool<TParams extends TSchema>. Runtime schema is correct.

export function registerSchedulerTools(pi: ExtensionAPI, bridge: GrpcBridge): void {
	const schedulerSchema = pi.zod.object({
		action: pi.zod.enum(["status", "trigger", "add", "remove"]).describe(
			"Scheduler action: status (snapshot), trigger (run a job now), add (new cron job), remove (delete a job)"
		),
		job_id: pi.zod.string().optional().describe(
			"Job ID (required for trigger and remove)"
		),
		description: pi.zod.string().optional().describe(
			"Human-readable job description (required for add)"
		),
		cron: pi.zod.string().optional().describe(
			"Cron expression, e.g. '0 2 * * *' (required for add)"
		),
		project_id: pi.zod.string().optional().describe(
			"Project ID to scope the job (optional for add)"
		),
		night_window_start: pi.zod.string().optional().describe(
			"Night-window start HH:MM (optional for add)"
		),
		night_window_end: pi.zod.string().optional().describe(
			"Night-window end HH:MM (optional for add)"
		),
		timezone: pi.zod.string().optional().describe(
			"IANA timezone, e.g. 'UTC' or 'America/New_York' (optional for add, default UTC)"
		),
		enabled: pi.zod.boolean().optional().describe(
			"Whether the new job is enabled (optional for add, default true)"
		),
	});

	pi.registerTool({
		name: "uc_scheduler",
		label: "UC Scheduler",
		description:
			"Manage the UltimateCoders cron scheduler. " +
			"Get scheduler status (running flag, night window, jobs, execution history), " +
			"trigger a job to run immediately, add a new cron job, or remove a job. " +
			"Note: add/remove may return UNIMPLEMENTED on servers that only accept " +
			"jobs declared in uc.scheduler.yaml.",
		parameters: schedulerSchema as never,
		async execute(_id, params: unknown, _signal, _onUpdate, _ctx) {
			const p = params as {
				action: string;
				job_id?: string;
				description?: string;
				cron?: string;
				project_id?: string;
				night_window_start?: string;
				night_window_end?: string;
				timezone?: string;
				enabled?: boolean;
			};
			try {
				switch (p.action) {
					case "status": {
						const status = await bridge.getSchedulerStatus();
						if (!status.available) {
							return {
								content: [{ type: "text" as const, text: "(scheduler service unavailable — gRPC server may be down)" }],
							};
						}
						const lines: string[] = [];
						lines.push(`Scheduler: ${status.isRunning ? "running" : "stopped"}`);
						if (status.nightWindow) {
							const nw = status.nightWindow;
							lines.push(`Night window: ${nw.start}-${nw.end} (${nw.enabled ? "enabled" : "disabled"})`);
						}
						if (status.jobs.length === 0) {
							lines.push("Jobs: (none)");
						} else {
							lines.push("Jobs:");
							for (const j of status.jobs) {
								const en = j.enabled ? "on" : "off";
								const last = j.lastRun ? ` last=${j.lastRun}` : "";
								const next = j.nextRun ? ` next=${j.nextRun}` : "";
								lines.push(`  [${en}] ${j.id}: ${j.name} (${j.cron})${last}${next}`);
							}
						}
						if (status.executionHistory.length > 0) {
							lines.push("Recent executions:");
							for (const h of status.executionHistory.slice(0, 10)) {
								const tag = h.status ?? (h.success ? "Completed" : "Failed");
								const err = h.error ? ` — ${h.error}` : "";
								lines.push(`  [${tag}] ${h.jobName} @ ${h.executedAt}${err}`);
							}
						}
						return { content: [{ type: "text" as const, text: lines.join("\n") }] };
					}
					case "trigger": {
						if (!p.job_id) {
							return {
								content: [{ type: "text" as const, text: "Error: job_id required for trigger" }],
								isError: true,
							};
						}
						const r = await bridge.triggerSchedulerJob(p.job_id);
						if (!r.success) {
							return {
								content: [{ type: "text" as const, text: `Trigger failed: ${r.error ?? "unknown error"}` }],
								isError: true,
							};
						}
						return {
							content: [{ type: "text" as const, text: `Triggered job ${r.jobId}` }],
						};
					}
					case "add": {
						if (!p.description || !p.cron) {
							return {
								content: [{ type: "text" as const, text: "Error: description and cron required for add" }],
								isError: true,
							};
						}
						const r = await bridge.addCronJob({
							description: p.description,
							cronExpression: p.cron,
							projectId: p.project_id,
							nightWindowStart: p.night_window_start,
							nightWindowEnd: p.night_window_end,
							timezone: p.timezone,
							enabled: p.enabled,
						});
						if (!r.ok) {
							return {
								content: [{ type: "text" as const, text: `Add cron job failed: ${r.error}` }],
								isError: true,
							};
						}
						return {
							content: [{ type: "text" as const, text: `Added cron job: ${r.jobId} (${p.cron})` }],
						};
					}
					case "remove": {
						if (!p.job_id) {
							return {
								content: [{ type: "text" as const, text: "Error: job_id required for remove" }],
								isError: true,
							};
						}
						const r = await bridge.removeJob(p.job_id);
						if (!r.ok) {
							return {
								content: [{ type: "text" as const, text: `Remove failed: ${r.error}` }],
								isError: true,
							};
						}
						return {
							content: [{ type: "text" as const, text: `Removed job ${p.job_id}` }],
						};
					}
					default:
						return {
							content: [{ type: "text" as const, text: `Unknown action: ${p.action}` }],
							isError: true,
						};
				}
			} catch (err) {
				return {
					content: [{
						type: "text" as const,
						text: `UC Scheduler error: ${err instanceof Error ? err.message : String(err)}`,
					}],
					isError: true,
				};
			}
		},
	});
}
