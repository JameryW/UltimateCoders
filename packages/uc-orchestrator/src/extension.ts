/**
 * UC Orchestrator Extension — Task orchestration for UltimateCoders.
 *
 * Registers:
 * - /uc submit <description>     — Submit a task for orchestration
 * - /uc status [task-id]         — Check task status (styled)
 * - /uc cancel <task-id> [<st>]  — Cancel task or specific subtask
 * - /uc pause <task-id>          — Pause task after current wave
 * - /uc resume <task-id>         — Resume a paused task
 * - /uc help                     — Show help
 *
 * Keyboard shortcuts:
 * - Ctrl+T        — Open SubtaskTree overlay
 * - Ctrl+Shift+T  — Open TaskList overlay
 * - Ctrl+Shift+F  — Open SubtaskTree on first failed subtask
 *
 * LLM-callable tools:
 * - uc_memory  — Read/write/search/delete UC layered memory
 * - uc_search  — Search UC hybrid index (text + semantic + AST)
 * - uc_task    — Task lifecycle: submit/cancel/pause/resume/status
 * - uc_index   — Index management: index_repo/list_repos/get_state/remove_index
 * - uc_file    — File operations: list_dir/get_file
 * - uc_worker  — Worker status: list workers, check capacity/heartbeat
 *
 * UI features:
 * - Rich progress widget above editor (real-time subtask progress)
 * - SubtaskTree overlay (Ctrl+T) with keyboard navigation
 * - TaskList overlay (Ctrl+Shift+T)
 * - Jump to first failed subtask (Ctrl+Shift+F)
 * - Custom message renderer for task results
 * - Connection status in footer
 */

import type { ExtensionAPI, ExtensionCommandContext, ExtensionContext } from "@oh-my-pi/pi-coding-agent";
import type { KeyId } from "@oh-my-pi/pi-tui";
import { UCOrchestrator, type TaskState } from "./orchestrator/orchestrator";
import { GrpcBridge } from "./orchestrator/grpc-bridge";
import { registerMemoryTools } from "./orchestrator/memory-bridge";
import { registerTaskTools, isSpawnDisabled } from "./orchestrator/task-bridge";
import { registerIndexTools } from "./orchestrator/index-bridge";
import { registerFileTools } from "./orchestrator/file-bridge";
import { registerWorkerTools } from "./orchestrator/worker-bridge";
import { createProgressWidget, type ProgressWidgetState, type SubtaskProgressInfo } from "./ui/progress-widget";
import { createSubtaskTreeOverlay } from "./ui/subtask-tree-overlay";
import { createTaskListOverlay } from "./ui/task-list-overlay";
import { createTaskResultRenderer } from "./ui/task-result-renderer";
import { FooterStatusRenderer, type StatusRenderer } from "./ui/status-renderer";
import { formatTaskList, formatTaskDetail } from "./ui/status-formatter";
import type { OrchestratorEventType, OrchestratorEvents } from "./orchestrator/events";

export default function ucOrchestratorExtension(pi: ExtensionAPI): void {
	pi.setLabel("UC Orchestrator");

	const bridge = new GrpcBridge();
	const orchestrator = new UCOrchestrator(pi, undefined, bridge);

	// ── Live state for widgets/overlays ─────────────────────────
	const progressState: Map<string, ProgressWidgetState> = new Map();
	let statusRenderer: StatusRenderer | undefined;

	// ── Restore persisted tasks on startup ──────────────────────
	orchestrator.restore().catch((err) => {
		pi.logger.warn(`Failed to restore tasks: ${err}`);
	});

	// ── Register message renderer for task results ──────────────
	pi.registerMessageRenderer("uc-task-result", createTaskResultRenderer());

	// ── Wire orchestrator events → UI updates ───────────────────
	pi.on("session_start", async (_event, ctx) => {
		// Clear stale handlers from a previous session that never got session_shutdown
		orchestrator.events.clear();

		statusRenderer = new FooterStatusRenderer(ctx.ui);
		statusRenderer.setField("conn", "UC: ready");

		const progressEvents: OrchestratorEventType[] = [
			"task_planning", "task_decomposed", "task_complete",
			"task_paused", "task_resumed", "task_cancelled",
			"wave_start", "wave_end",
			"subtask_start", "subtask_end", "subtask_failed", "subtask_reviewing",
			"subtask_progress",
			"connection_state",
		];

		for (const type of progressEvents) {
			orchestrator.events.on(type, (data) => {
				handleOrchestratorEvent(type, data, ctx as unknown as ExtensionCommandContext);
			});
		}
	});

	pi.on("session_shutdown", async () => {
		await orchestrator.destroy();
		progressState.clear();
	});

	// ── Event handler ───────────────────────────────────────────
	function handleOrchestratorEvent(
		type: OrchestratorEventType,
		data: OrchestratorEvents[OrchestratorEventType],
		ctx: ExtensionCommandContext,
	): void {
		switch (type) {
			case "task_planning": {
				const d = data as OrchestratorEvents["task_planning"];
				progressState.set(d.taskId, { task: getTaskOrEmpty(d.taskId) });
				ctx.ui.setWorkingMessage(`UC: Planning ${d.taskId.slice(0, 8)}...`);
				statusRenderer?.setField("active", `UC: planning`);
				break;
			}
			case "task_decomposed": {
				const d = data as OrchestratorEvents["task_decomposed"];
				ctx.ui.setWorkingMessage(`UC: ${d.subtaskCount} subtasks, ${d.waveCount} waves`);
				break;
			}
			case "wave_start": {
				const d = data as OrchestratorEvents["wave_start"];
				updateProgressState(d.taskId, { waveIdx: d.waveIdx, totalWaves: d.totalWaves });
				ctx.ui.setWorkingMessage(`UC: Wave ${d.waveIdx + 1}/${d.totalWaves}`);
				break;
			}
			case "subtask_start": {
				const d = data as OrchestratorEvents["subtask_start"];
				ctx.ui.setWorkingMessage(`UC: ${d.description.slice(0, 40)}`);
				// ponytail: refresh the widget's task snapshot so the now-running
				// subtask shows in the "running" list immediately. Without this, ps.task
				// stayed stale from task_planning until subtask_end — the running row
				// was invisible for the subtask's whole lifetime if no progress event fired.
				const task = orchestrator.getTaskState(d.taskId);
				if (task) {
					const ps = progressState.get(d.taskId);
					if (ps) {
						ps.task = task;
						ctx.ui.setWidget(`uc-${d.taskId}`, createProgressWidget(() => ps));
					}
				}
				break;
			}
			case "subtask_end":
			case "subtask_failed":
			case "subtask_reviewing": {
				const d = data as OrchestratorEvents["subtask_end"] | OrchestratorEvents["subtask_failed"];
				const task = orchestrator.getTaskState(d.taskId);
				if (task) {
					const ps = progressState.get(d.taskId);
					if (ps) {
						ps.task = task;
						// Clear progress entry for terminal subtasks (completed/failed)
						ps.progressBySubtask?.delete(d.subtaskId);
						ctx.ui.setWidget(`uc-${d.taskId}`, createProgressWidget(() => ps));
					}
				}
				break;
			}
			case "subtask_progress": {
				const d = data as OrchestratorEvents["subtask_progress"];
				let ps = progressState.get(d.taskId);
				if (!ps) {
					ps = { task: getTaskOrEmpty(d.taskId) };
					progressState.set(d.taskId, ps);
				}
				if (!ps.progressBySubtask) {
					ps.progressBySubtask = new Map<string, SubtaskProgressInfo>();
				}
				const info: SubtaskProgressInfo = {
					phase: d.phase,
					percent: d.percent,
				};
				if (d.stepIndex !== undefined) info.stepIndex = d.stepIndex;
				if (d.stepTotal !== undefined) info.stepTotal = d.stepTotal;
				if (d.stepAgent !== undefined) info.stepAgent = d.stepAgent;
				if (d.stepStatus !== undefined) info.stepStatus = d.stepStatus;
				if (d.stepSummary !== undefined) info.stepSummary = d.stepSummary;
				if (d.parallelGroup !== undefined) info.parallelGroup = d.parallelGroup;
				if (d.parallelStepCount !== undefined) info.parallelStepCount = d.parallelStepCount;
				ps.progressBySubtask.set(d.subtaskId, info);
				ctx.ui.setWidget(`uc-${d.taskId}`, createProgressWidget(() => ps!));
				break;
			}
			case "wave_end": {
				const d = data as OrchestratorEvents["wave_end"];
				updateProgressState(d.taskId, { waveIdx: d.waveIdx, totalWaves: d.totalWaves });
				break;
			}
			case "task_complete": {
				const d = data as OrchestratorEvents["task_complete"];
				ctx.ui.setWidget(`uc-${d.taskId}`, undefined);
				progressState.delete(d.taskId);
				ctx.ui.setWorkingMessage(undefined);
				statusRenderer?.setField("active", `UC: ${d.status}`);
				break;
			}
			case "task_paused":
			case "task_resumed":
			case "task_cancelled": {
				statusRenderer?.setField("active", `UC: ${type.replace("task_", "")}`);
				break;
			}
			case "connection_state": {
				const d = data as OrchestratorEvents["connection_state"];
				statusRenderer?.setField("conn", d.connected ? "UC: connected" : "UC: disconnected");
				break;
			}
		}
	}

	function getTaskOrEmpty(taskId: string): TaskState {
		return orchestrator.getTaskState(taskId) ?? {
			id: taskId, description: "", status: "planning", controlState: "running",
			subtasks: [], createdAt: Date.now(),
		};
	}

	function updateProgressState(taskId: string, update: Partial<ProgressWidgetState>): void {
		const ps = progressState.get(taskId);
		if (ps) Object.assign(ps, update);
	}

	// ── Keyboard shortcuts ──────────────────────────────────────

	// ponytail: shared task-list opener — used by Ctrl+Shift+T (list) and the
	// subtask-tree `d` jump (lands in detail via initialDetailTaskId).
	async function openTaskList(ctx: ExtensionCommandContext, initialDetailTaskId?: string) {
		await ctx.ui.custom(
			createTaskListOverlay({
				tasks: () => orchestrator.getAllTaskStates(),
				getTask: (taskId) => orchestrator.getTaskState(taskId),
				onAction: async (taskId, action) => {
					// c/p/r quick actions — mirrors /uc cancel|pause|resume.
					const ok = action === "cancel"
						? await orchestrator.cancelTask(taskId, undefined, ctx as unknown as ExtensionCommandContext)
						: action === "pause"
							? await orchestrator.pauseTask(taskId, ctx as unknown as ExtensionCommandContext)
							: await orchestrator.resumeTask(taskId, ctx as unknown as ExtensionCommandContext);
					// ponytail: return ok so the overlay can show in-overlay confirmation
					// (flashMsg) on success; surface failure via notify (warning toast).
					if (!ok) {
						ctx.ui.notify(`Cannot ${action} task ${taskId.slice(0, 8)}: wrong state`, "warning");
					}
					return ok;
				},
				initialDetailTaskId,
				onClose: () => {},
			}),
			{ overlay: true },
		);
	}

	pi.registerShortcut("ctrl+t" as KeyId, {
		description: "Open UC subtask tree",
		handler: async (ctx) => {
			await ctx.ui.custom(
				createSubtaskTreeOverlay({
					tasks: () => orchestrator.getAllTaskStates(),
					onRetry: async (taskId, subtaskId) => {
						// Per-subtask retry: reset + re-dispatch ONLY the cursor's failed
						// subtask (+ its cascade-cancelled downstream), leaving other
						// failed subtasks untouched. Distinct from task-scoped resumeTask.
						const ok = await orchestrator.retrySubtask(taskId, subtaskId, ctx as unknown as ExtensionCommandContext);
						if (ok) {
							ctx.ui.notify(`Retrying subtask ${subtaskId.slice(0, 8)} — re-dispatched`, "info");
						} else {
							ctx.ui.notify(`Cannot retry ${subtaskId.slice(0, 8)}: not a failed subtask (or deps incomplete)`, "warning");
						}
					},
					onJumpToTask: (taskId) => {
						// `d` — close tree (done() is called by the overlay) then open the
						// task-list straight into that task's detail.
						void openTaskList(ctx as unknown as ExtensionCommandContext, taskId);
					},
					onClose: () => {},
				}),
				{ overlay: true },
			);
		},
	});

	pi.registerShortcut("ctrl+shift+t" as KeyId, {
		description: "Open UC task list",
		handler: async (ctx) => {
			await openTaskList(ctx as unknown as ExtensionCommandContext);
		},
	});

	pi.registerShortcut("ctrl+shift+f" as KeyId, {
		description: "Jump to first failed subtask",
		handler: async (ctx) => {
			// ponytail: open the subtask tree with the cursor pre-set to the first
			// failed subtask — fastest path to the `R` retry key. No failed → toast.
			const hasFailed = orchestrator.getAllTaskStates().some((t) =>
				t.subtasks.some((s) => s.status === "failed"));
			if (!hasFailed) {
				ctx.ui.notify("No failed subtasks", "info");
				return;
			}
			await ctx.ui.custom(
				createSubtaskTreeOverlay({
					tasks: () => orchestrator.getAllTaskStates(),
					cursorOnFailed: true,
					onRetry: async (taskId, subtaskId) => {
						const ok = await orchestrator.retrySubtask(taskId, subtaskId, ctx as unknown as ExtensionCommandContext);
						if (ok) {
							ctx.ui.notify(`Retrying subtask ${subtaskId.slice(0, 8)} — re-dispatched`, "info");
						} else {
							ctx.ui.notify(`Cannot retry ${subtaskId.slice(0, 8)}: not a failed subtask (or deps incomplete)`, "warning");
						}
					},
					onJumpToTask: (taskId) => {
						void openTaskList(ctx as unknown as ExtensionCommandContext, taskId);
					},
					onClose: () => {},
				}),
				{ overlay: true },
			);
		},
	});

	// ── /uc command ─────────────────────────────────────────────

	const SUBCOMMANDS = ["submit", "status", "cancel", "pause", "resume", "search", "help"];

	pi.registerCommand("uc", {
		description: "UltimateCoders task orchestration",
		getArgumentCompletions: (prefix: string) => {
			if (!prefix) return SUBCOMMANDS.map((s) => ({ label: s, value: s }));
			return SUBCOMMANDS
				.filter((s) => s.startsWith(prefix))
				.map((s) => ({ label: s, value: s }));
		},
		handler: async (args: string, ctx: ExtensionCommandContext) => {
			const parts = args.trim().split(/\s+/);
			const subcommand = parts[0] ?? "help";
			const rest = parts.slice(1).join(" ");

			switch (subcommand) {
				case "submit": {
					if (!rest) {
						ctx.ui.notify("Usage: /uc submit <task description>", "error");
						return;
					}
					if (isSpawnDisabled()) {
						ctx.ui.notify("子任务派发已禁用 (UC_NO_SPAWN)。用 /uc status 查看已有任务。", "error");
						return;
					}
					await orchestrator.submitTask(rest, ctx);
					return;
				}
				case "status": {
					const taskId = rest.trim() || undefined;
					if (!taskId) {
						const tasks = orchestrator.getAllTaskStates();
						const lines = formatTaskList(tasks, ctx.ui.theme);
						ctx.ui.notify(lines.join("\n"), "info");
					} else {
						const task = orchestrator.getTaskState(taskId);
						if (!task) {
							ctx.ui.notify(`Task ${taskId} not found`, "error");
							return;
						}
						const lines = formatTaskDetail(task, ctx.ui.theme);
						ctx.ui.notify(lines.join("\n"), "info");
					}
					return;
				}
				case "cancel": {
					const cancelParts = rest.trim().split(/\s+/);
					const tid = cancelParts[0];
					const subtaskId = cancelParts[1];
					if (!tid) {
						ctx.ui.notify("Usage: /uc cancel <task-id> [<subtask-id>]", "error");
						return;
					}
					const ok = await orchestrator.cancelTask(tid, subtaskId, ctx);
					if (!ok) {
						ctx.ui.notify(`Cancel failed: task ${tid} not found`, "error");
					}
					return;
				}
				case "pause": {
					const tid = rest.trim();
					if (!tid) {
						ctx.ui.notify("Usage: /uc pause <task-id>", "error");
						return;
					}
					const ok = await orchestrator.pauseTask(tid, ctx);
					if (!ok) {
						ctx.ui.notify(`Pause failed: task ${tid} not found or not in progress`, "error");
					}
					return;
				}
				case "resume": {
					const tid = rest.trim();
					if (!tid) {
						ctx.ui.notify("Usage: /uc resume <task-id>", "error");
						return;
					}
					const ok = await orchestrator.resumeTask(tid, ctx);
					if (!ok) {
						ctx.ui.notify(`Resume failed: task ${tid} not found or not paused/failed`, "error");
					}
					return;
				}
				case "search": {
					if (!rest) {
						ctx.ui.notify("Usage: /uc search <query>", "error");
						return;
					}
					try {
						const results = await bridge.searchCode(rest);
						if (!results || results.length === 0) {
							ctx.ui.notify("No results found.", "info");
							return;
						}
						// ponytail: notify() is a toast with no ANSI-aware truncation
						// backstop (unlike the overlay compositor), so cap each line's
						// plain content to the live terminal width. Path/snippet are plain
						// text — slice before building the line. Undefined cols (headless)
						// keeps the legacy fixed caps (80 path / 120 snippet).
						const cols = (ctx.ui as any)?.terminal?.columns as number | undefined;
						const SHOWN = 20;
						const shown = results.slice(0, SHOWN);
						const lines = shown.map(
							(r: any) => {
								const repo = r.repoId ?? r.repo_id ?? "?";
								const path = r.filePath ?? r.file_path ?? "?";
								const score = r.score ? ` (${r.score.toFixed(2)})` : "";
								const snippet = (r.snippet ?? "").replace(/\s+/g, " ").trim();
								// `  [repo] path score\n      snippet` — the path line prefix
								// is `  [repo] ` (~6 + repo) + score; cap path so it fits.
								const pathPrefix = `  [${repo}] `;
								const pathBudget = cols !== undefined
									? Math.max(0, cols - pathPrefix.length - (r.score ? score.length : 0))
									: 80;
								const pathStr = path.length > pathBudget
									? path.slice(0, Math.max(0, pathBudget - 1)) + "…"
									: path;
								// snippet line prefix is 6 spaces; cap the snippet body.
								const snipBudget = cols !== undefined ? Math.max(0, cols - 6) : 120;
								const snip = snippet
									? `\n      ${snippet.length > snipBudget ? snippet.slice(0, Math.max(0, snipBudget - 1)) + "…" : snippet}`
									: "";
								return `${pathPrefix}${pathStr}${score}${snip}`;
							},
						);
						// ponytail: if we truncated the result set, say so — "Found 50"
						// while only listing 20 misled users into thinking the rest were
						// lost or that 20 was the total.
						const header = results.length > SHOWN
							? `Found ${results.length} result(s) — showing first ${SHOWN}:`
							: `Found ${results.length} result(s):`;
						ctx.ui.notify([header, ...lines].join("\n"), "info");
					} catch (e) {
						ctx.ui.notify(`Search failed: ${e}`, "error");
					}
					return;
				}
					default:
					ctx.ui.notify(
						[
							"UC Orchestrator — distributed AI coding orchestration",
							"",
							"  /uc submit <description>       Submit a task",
							"  /uc status [task-id]           Check task status",
							"  /uc cancel <task-id> [<st-id>] Cancel task or subtask",
							"  /uc pause <task-id>            Pause after current wave",
							"  /uc resume <task-id>           Resume a paused or failed task",
							"  /uc search <query>             Search across indexed repos",
							"  /uc help                       Show this help",
							"",
							"Shortcuts:",
							"  Ctrl+T         Subtask tree overlay",
							"  Ctrl+Shift+T   Task list overlay",
							"  Ctrl+Shift+F   Jump to first failed subtask",
						].join("\n"),
						"info",
					);
					return;
			}
		},
	});

	// ── LLM-callable tools ─────────────────────────────────────
	registerMemoryTools(pi, bridge);
	registerTaskTools(pi, bridge, orchestrator);
	registerIndexTools(pi, bridge);
	registerFileTools(pi, bridge);
	registerWorkerTools(pi, bridge);
}
