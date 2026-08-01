/**
 * UC Orchestrator Extension — Task orchestration for UltimateCoders.
 *
 * Registers:
 * - /uc submit <description>     — Submit a task for orchestration
 * - /uc status [task-id]         — Check task status (styled)
 * - /uc cancel <task-id> [<st>]  — Cancel task or specific subtask
 * - /uc pause <task-id>          — Pause task after current wave
 * - /uc resume <task-id>         — Resume a paused task
 * - /uc search <query>           — Search across indexed repos
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
import { formatTaskList, formatTaskDetail, sortTasksForStatus, highlightQuery } from "./ui/status-formatter";
import type { OrchestratorEventType, OrchestratorEvents } from "./orchestrator/events";

export default function ucOrchestratorExtension(pi: ExtensionAPI): void {
	pi.setLabel("UC Orchestrator");

	const bridge = new GrpcBridge();
	const orchestrator = new UCOrchestrator(pi, undefined, bridge);

	// ── Live state for widgets/overlays ─────────────────────────
	const progressState: Map<string, ProgressWidgetState> = new Map();
	let statusRenderer: StatusRenderer | undefined;
	// ponytail: F31 — the working message is one global slot; tasks run
	// concurrently (RPC fire-and-forget, restored tasks), so task_complete must
	// only clear the message if it still belongs to the completing task.
	let lastWorkingTaskId: string | null = null;

	// ── Restore persisted tasks on startup ──────────────────────
	orchestrator.restore().catch((err) => {
		pi.logger.warn(`Failed to restore tasks: ${err}`);
	});

	// ── Register message renderer for task results ──────────────
	// ponytail: F15 — the renderer resolves the live task snapshot via getter
	// (message details carry only taskId/status/subtaskCount), so expanding a
	// task-result message shows its subtask rows.
	pi.registerMessageRenderer("uc-task-result", createTaskResultRenderer((id) => orchestrator.getTaskState(id)));

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
			"reconnect_progress",
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

	// ponytail: F27 — per-reason failure messages for /uc status|cancel|pause|
	// resume. The old toasts were hardcoded ("task not found") whatever the
	// cause, so a typo'd subtask id blamed the task and an ambiguous prefix
	// gave no way forward. `candidates` are the matches or recent task ids.
	// ponytail: recent task ids (newest-first, ≤5) for usage hints — mirrors the
	// candidates the resolver already surfaces on a not_found/ambiguous failure,
	// so a /uc cancel|pause|resume with NO id names what's available instead of a
	// bare "Usage:" the user can't act on without a separate /uc status.
	function recentTaskIds(): string[] {
		return orchestrator.getAllTaskStates()
			.slice()
			.sort((a, b) => b.createdAt - a.createdAt)
			.slice(0, 5)
			.map((t) => t.id);
	}
	function controlFailureMessage(
		verb: string,
		tid: string,
		r: Extract<import("./orchestrator/orchestrator").ControlOutcome, { ok: false }>,
		currentStatus?: string,
	): string {
		const list = (r.candidates ?? []).join(", ") || "(none)";
		switch (r.reason) {
			case "not_found":
				return `${verb} failed: no task matches "${tid}". Recent tasks: ${list}`;
			case "ambiguous":
				return `${verb} failed: "${tid}" matches multiple tasks: ${list}`;
			case "subtask_not_found":
				return `${verb} failed: no such subtask in task. Subtasks: ${list}`;
			case "bad_state":
				// ponytail: name the current state so the user knows WHY the verb was
				// rejected (was "not in a cancel-able state" — no hint whether the task
				// was completed/paused/failed). currentStatus is re-resolved at the call
				// site; absent only when resolveTask itself failed (then bad_state wouldn't
				// fire — task exists for bad_state), so the fallback stays generic.
				return currentStatus
					? `${verb} failed: task "${tid}" is ${currentStatus}, not ${verb.toLowerCase()}-able`
					: `${verb} failed: task "${tid}" is not in a ${verb.toLowerCase()}-able state`;
		}
	}

	// ponytail: re-resolve the task's CURRENT status for the bad_state failure
	// message — cancelTask/pauseTask/resumeTask reject on state, so the task
	// exists; resolveTask(idOrPrefix) returns it (or a ControlOutcome if the
	// prefix itself was ambiguous/not_found, in which case bad_state wouldn't
	// have fired). controlState superseded by status when it differs (paused
	// keeps status=in_progress, so prefer the badge the user sees: controlState).
	function currentStatusFor(tid: string): string | undefined {
		const r = orchestrator.resolveTask(tid);
		if ("status" in r) return r.controlState && r.controlState !== "running" ? `${r.status} [${r.controlState}]` : r.status;
		return undefined;
	}

	function handleOrchestratorEvent(
		type: OrchestratorEventType,
		data: OrchestratorEvents[OrchestratorEventType],
		ctx: ExtensionCommandContext,
	): void {
		switch (type) {
			case "task_planning": {
				const d = data as OrchestratorEvents["task_planning"];
				progressState.set(d.taskId, { task: getTaskOrEmpty(d.taskId) });
				lastWorkingTaskId = d.taskId;
				ctx.ui.setWorkingMessage(`UC: Planning ${d.taskId.slice(0, 8)}...`);
				// ponytail: F32 — attribute the footer field so concurrent tasks
				// don't read as one anonymous "UC: planning".
				statusRenderer?.setField("active", `UC: ${d.taskId.slice(0, 8)} · planning`);
				break;
			}
			case "task_decomposed": {
				const d = data as OrchestratorEvents["task_decomposed"];
				lastWorkingTaskId = d.taskId;
				ctx.ui.setWorkingMessage(`UC: ${d.subtaskCount} subtasks, ${d.waveCount} waves`);
				break;
			}
			case "wave_start": {
				const d = data as OrchestratorEvents["wave_start"];
				updateProgressState(d.taskId, { waveIdx: d.waveIdx, totalWaves: d.totalWaves });
				lastWorkingTaskId = d.taskId;
				ctx.ui.setWorkingMessage(`UC: Wave ${d.waveIdx + 1}/${d.totalWaves}`);
				break;
			}
			case "subtask_start": {
				const d = data as OrchestratorEvents["subtask_start"];
				lastWorkingTaskId = d.taskId;
				ctx.ui.setWorkingMessage(`UC: ${d.description.slice(0, 40)}`);
				// ponytail: refresh the widget's task snapshot so the now-running
				// subtask shows in the "running" list immediately. Without this, ps.task
				// stayed stale from task_planning until subtask_end — the running row
				// was invisible for the subtask's whole lifetime if no progress event fired.
				const task = orchestrator.getTaskState(d.taskId);
				if (task) {
					// ponytail: F14 — create the state if missing. Entries were only
					// created by task_planning (new submits) and subtask_progress
					// (remote workers), so resumed tasks and local-execution subtasks
					// never got the rich widget at all.
					let ps = progressState.get(d.taskId);
					if (!ps) {
						ps = { task: getTaskOrEmpty(d.taskId) };
						progressState.set(d.taskId, ps);
					}
					ps.task = task;
					// ponytail: F19 — seed the progress entry at dispatch so elapsed
					// starts now (not at the first progress event — local subtasks
					// never emit those, so without this they'd get no elapsed at all).
					// percent -1 = no data yet; the widget skips negatives. Don't
					// clobber an existing entry (retries re-fire subtask_start).
					if (!ps.progressBySubtask) ps.progressBySubtask = new Map<string, SubtaskProgressInfo>();
					if (!ps.progressBySubtask.has(d.subtaskId)) {
						ps.progressBySubtask.set(d.subtaskId, { phase: "starting", percent: -1, firstSeen: Date.now() });
					}
					ctx.ui.setWidget(`uc-${d.taskId}`, createProgressWidget(() => ps!));
				}
				break;
			}
			case "subtask_end":
			case "subtask_failed": {
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
			case "subtask_reviewing": {
				// ponytail: F12 — reviewing is NOT terminal: keep the progress entry
				// (agent/step/percent tags stay visible during review). The old shared
				// end/failed/reviewing branch deleted it, blanking the live tags.
				const d = data as OrchestratorEvents["subtask_reviewing"];
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
				// ponytail: F19 — carry firstSeen across the wholesale replace so
				// elapsed keeps counting from dispatch (seeded at subtask_start).
				info.firstSeen = ps.progressBySubtask.get(d.subtaskId)?.firstSeen ?? Date.now();
				ps.progressBySubtask.set(d.subtaskId, info);
				ctx.ui.setWidget(`uc-${d.taskId}`, createProgressWidget(() => ps!));
				break;
			}
			case "wave_end": {
				const d = data as OrchestratorEvents["wave_end"];
				updateProgressState(d.taskId, { waveIdx: d.waveIdx, totalWaves: d.totalWaves });
				// ponytail: F12 — refresh the widget so completed-wave rows render
				// immediately. Before, state changed but nothing re-rendered until
				// the next subtask event (the wave row stayed stale).
				const task = orchestrator.getTaskState(d.taskId);
				const ps = progressState.get(d.taskId);
				if (task && ps) {
					ps.task = task;
					ctx.ui.setWidget(`uc-${d.taskId}`, createProgressWidget(() => ps));
				}
				break;
			}
			case "task_complete": {
				const d = data as OrchestratorEvents["task_complete"];
				ctx.ui.setWidget(`uc-${d.taskId}`, undefined);
				progressState.delete(d.taskId);
				// ponytail: F31 — only clear the working message if it still belongs
				// to this task; a concurrent task's "UC: Wave 2/3" must survive.
				if (lastWorkingTaskId === d.taskId) {
					ctx.ui.setWorkingMessage(undefined);
					lastWorkingTaskId = null;
				}
				// ponytail: F32 — attribute the terminal state, then hand the footer
				// slot to a still-active task if one exists (else clear it — the
				// field was never cleared before, so "UC: failed" showed all session).
				const stillActive = orchestrator.getAllTaskStates().filter(
					(t) => t.status === "in_progress" || t.status === "planning",
				);
				statusRenderer?.setField(
					"active",
					stillActive.length > 0
						? `UC: ${stillActive[0].id.slice(0, 8)} · ${stillActive[0].status}`
						: `UC: ${d.taskId.slice(0, 8)} · ${d.status}`,
				);
				break;
			}
			case "task_paused":
			case "task_resumed":
			case "task_cancelled": {
				// ponytail: F32 — attribute; events all carry taskId.
				const d = data as OrchestratorEvents["task_paused"];
				statusRenderer?.setField("active", `UC: ${d.taskId.slice(0, 8)} · ${type.replace("task_", "")}`);
				break;
			}
			case "connection_state": {
				const d = data as OrchestratorEvents["connection_state"];
				statusRenderer?.setField("conn", d.connected ? "UC: connected" : "UC: disconnected");
				break;
			}
			case "reconnect_progress": {
				// ponytail: F10 — live reconnect countdown in the footer. Each backoff
				// wait fires this once; connection_state(true) overwrites it back to
				// "UC: connected" when the bridge recovers.
				const d = data as OrchestratorEvents["reconnect_progress"];
				const secs = Math.max(1, Math.ceil(d.nextRetryMs / 1000));
				statusRenderer?.setField("conn", `UC: reconnecting · try ${d.attempt} · ${secs}s`);
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

	// ponytail: shared subtask-tree opener — used by Ctrl+T, Ctrl+Shift+F
	// (cursorOnFailed), and the task-list detail `t` jump (cursorOnTaskId).
	// One builder so the onRetry/onJumpToTask wiring stays identical across all
	// three entry points; previously the tree was re-built inline twice.
	async function openSubtaskTree(
		ctx: ExtensionCommandContext,
		opts?: { cursorOnFailed?: boolean; cursorOnTaskId?: string },
	) {
		await ctx.ui.custom(
			createSubtaskTreeOverlay({
				tasks: () => orchestrator.getAllTaskStates(),
				cursorOnFailed: opts?.cursorOnFailed,
				cursorOnTaskId: opts?.cursorOnTaskId,
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
	}

	// ponytail: shared task-list opener — used by Ctrl+Shift+T (list) and the
	// subtask-tree `d` jump (lands in detail via initialDetailTaskId).
	async function openTaskList(ctx: ExtensionCommandContext, initialDetailTaskId?: string) {
		await ctx.ui.custom(
			createTaskListOverlay({
				tasks: () => orchestrator.getAllTaskStates(),
				getTask: (taskId) => orchestrator.getTaskState(taskId),
				onAction: async (taskId, action) => {
					// c/p/r quick actions — mirrors /uc cancel|pause|resume.
					const r = action === "cancel"
						? await orchestrator.cancelTask(taskId, undefined, ctx as unknown as ExtensionCommandContext)
						: action === "pause"
							? await orchestrator.pauseTask(taskId, ctx as unknown as ExtensionCommandContext)
							: await orchestrator.resumeTask(taskId, ctx as unknown as ExtensionCommandContext);
					// ponytail: return ok so the overlay can show in-overlay confirmation
					// (flashMsg) on success; surface failure via notify (warning toast).
					if (!r.ok) {
						ctx.ui.notify(`Cannot ${action} task ${taskId.slice(0, 8)}: ${r.reason === "bad_state" ? "wrong state" : r.reason}`, "warning");
					}
					return r.ok;
				},
				initialDetailTaskId,
				// ponytail: `t` in detail opens the subtask tree on this task — the
				// reverse of the tree's `d` → detail jump. Close the list first so
				// overlays don't stack (the overlay calls done() before onJumpToTree).
				onJumpToTree: (taskId) => {
					void openSubtaskTree(ctx as unknown as ExtensionCommandContext, { cursorOnTaskId: taskId });
				},
				onClose: () => {},
			}),
			{ overlay: true },
		);
	}

	pi.registerShortcut("ctrl+t" as KeyId, {
		description: "Open UC subtask tree",
		handler: async (ctx) => {
			await openSubtaskTree(ctx as unknown as ExtensionCommandContext);
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
			// failed subtask — fastest path to the `R` retry key. No failed subtask →
			// but distinguish: a task-level failure with NO subtasks (decompose failed
			// before any ran) reads as "failed" to the user, so "No failed subtasks" alone
			// was confusing — the user saw a failed task and a toast denying any. Point
			// them at the task-level recovery path (/uc resume, or the list's `r`) instead.
			const hasFailedSub = orchestrator.getAllTaskStates().some((t) =>
				t.subtasks.some((s) => s.status === "failed"));
			if (!hasFailedSub) {
				const hasFailedTask = orchestrator.getAllTaskStates().some((t) => t.status === "failed");
				ctx.ui.notify(
					hasFailedTask
						? "No failed subtasks — use /uc resume <task-id> (or `r` in the task list) for a task-level failure"
						: "No failed subtasks",
					"info",
				);
				return;
			}
			await openSubtaskTree(ctx as unknown as ExtensionCommandContext, { cursorOnFailed: true });
		},
	});

	// ── /uc command ─────────────────────────────────────────────

	const SUBCOMMANDS = ["submit", "status", "cancel", "pause", "resume", "search", "help"];

	pi.registerCommand("uc", {
		description: "UltimateCoders task orchestration",
		getArgumentCompletions: (prefix: string) => {
			// ponytail: F33 — past the first word, complete TASK IDS for the
			// commands that take one (ids are long and only ever displayed
			// truncated — completion is the usability win next to F26 prefix
			// matching). cancel's 3rd word (subtask id) is left out: low freq.
			const words = prefix.split(/\s+/);
			if (words.length >= 2 && ["status", "cancel", "pause", "resume"].includes(words[0])) {
				const frag = words[words.length - 1];
				return orchestrator.getAllTaskStates()
					.filter((t) => t.id.startsWith(frag))
					.slice(0, 10)
					.map((t) => ({ label: `${t.id.slice(0, 14)} (${t.status})`, value: t.id }));
			}
			const first = words[0] ?? "";
			if (!prefix) return SUBCOMMANDS.map((s) => ({ label: s, value: s }));
			return SUBCOMMANDS
				.filter((s) => s.startsWith(first))
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
						// ponytail: F38 — English like every other user-facing string.
					ctx.ui.notify("Subtask spawning disabled (UC_NO_SPAWN). Use /uc status to view existing tasks.", "error");
						return;
					}
					// ponytail: immediate confirmation — submitTask returns the task id, but
					// the call site discarded it. The user saw only the async "Planning..."
					// footer, with no confirmation the command landed. Show the task id so the
					// user can /uc status <id> immediately. Mirrors #469's success-notify pattern.
					const submittedId = await orchestrator.submitTask(rest, ctx);
					ctx.ui.notify(`Submitted task ${submittedId.slice(0, 8)} — /uc status ${submittedId.slice(0, 8)} for detail`, "info");
					return;
				}
				case "status": {
					const taskId = rest.trim() || undefined;
					// ponytail: pass live terminal columns so /uc status (notify toast,
					// no overlay compositor truncation backstop) caps long desc/error
					// lines to the actual width instead of fixed 50/60/100.
					const cols = (ctx.ui as any)?.terminal?.columns;
					if (!taskId) {
						// ponytail: sort active/failed tasks first — getAllTaskStates returns
						// Map insertion order (oldest first), so a /uc status toast with many
						// completed tasks buried the live/failed tasks a user is watching at
						// the bottom of a fixed toast. Toast is fixed-height, so surfacing
						// the actionable tasks up top is the difference between seen and
						// scrolled-off. sortTasksForStatus is shared w/ the selfcheck.
						const tasks = sortTasksForStatus(orchestrator.getAllTaskStates());
						const lines = formatTaskList(tasks, ctx.ui.theme, cols);
						// ponytail: hint the detail path — the list shows truncated ids +
						// a status each, but a new user has no cue that /uc status <id>
						// expands one. Append only when there are tasks (empty list already
						// says "No tasks"); one dim line, cheap to skip on narrow terminals.
						if (tasks.length > 0) {
							lines.push(ctx.ui.theme.fg("dim", "/uc status <task-id> for detail"));
						}
						ctx.ui.notify(lines.join("\n"), "info");
					} else {
						// ponytail: F26 — prefix resolution; UI only shows truncated ids.
						// resolveTask returns a task or a failure outcome (never ok:true).
						const resolved = orchestrator.resolveTask(taskId);
						if ("ok" in resolved) {
							if (!resolved.ok) {
								ctx.ui.notify(controlFailureMessage("Status", taskId, resolved), "error");
							}
							return;
						}
						const lines = formatTaskDetail(resolved, ctx.ui.theme, cols, 10);
						ctx.ui.notify(lines.join("\n"), "info");
					}
					return;
				}
				case "cancel": {
					const cancelParts = rest.trim().split(/\s+/);
					const tid = cancelParts[0];
					const subtaskId = cancelParts[1];
					if (!tid) {
						const recent = recentTaskIds().join(", ") || "(none)";
						ctx.ui.notify(`Usage: /uc cancel <task-id> [<subtask-id>]\nRecent tasks: ${recent}`, "error");
						return;
					}
					const r = await orchestrator.cancelTask(tid, subtaskId, ctx);
					if (!r.ok) {
						ctx.ui.notify(controlFailureMessage("Cancel", tid, r, currentStatusFor(tid)), "error");
					} else {
						// ponytail: success feedback — without this, /uc cancel was silent on
						// success. The task_cancelled event fires later (async footer update),
						// but the user had no immediate confirmation the command landed.
						ctx.ui.notify(subtaskId
							? `Cancelled subtask ${subtaskId.slice(0, 8)} in task ${tid.slice(0, 8)}`
							: `Cancelled task ${tid.slice(0, 8)}`, "info");
					}
					return;
				}
				case "pause": {
					// ponytail: F29 — take the first token (cancel already did; pause/
					// resume took the whole remainder, so "/uc pause uc-1 why" failed).
					const tid = rest.trim().split(/\s+/)[0];
					if (!tid) {
						const recent = recentTaskIds().join(", ") || "(none)";
						ctx.ui.notify(`Usage: /uc pause <task-id>\nRecent tasks: ${recent}`, "error");
						return;
					}
					const r = await orchestrator.pauseTask(tid, ctx);
					if (!r.ok) {
						ctx.ui.notify(controlFailureMessage("Pause", tid, r, currentStatusFor(tid)), "error");
					} else {
						ctx.ui.notify(`Paused task ${tid.slice(0, 8)}`, "info");
					}
					return;
				}
				case "resume": {
					const tid = rest.trim().split(/\s+/)[0];
					if (!tid) {
						const recent = recentTaskIds().join(", ") || "(none)";
						ctx.ui.notify(`Usage: /uc resume <task-id>\nRecent tasks: ${recent}`, "error");
						return;
					}
					const r = await orchestrator.resumeTask(tid, ctx);
					if (!r.ok) {
						ctx.ui.notify(controlFailureMessage("Resume", tid, r, currentStatusFor(tid)), "error");
					} else {
						ctx.ui.notify(`Resumed task ${tid.slice(0, 8)}`, "info");
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
						// ponytail: sort by score DESC before capping — the server usually returns
						// relevance-sorted, but a defensive client sort guarantees the top-20 slice
						// holds the highest-score results (a server that didn't sort would otherwise
						// bury the best matches past the cap). Missing score sorts last (0 fallback).
						const sorted = results.slice().sort((a: any, b: any) => (b.score ?? 0) - (a.score ?? 0));
						const shown = sorted.slice(0, SHOWN);
						const lines = shown.map(
							(r: any) => {
								const repo = r.repoId ?? r.repo_id ?? "?";
								const path = r.filePath ?? r.file_path ?? "?";
								const score = r.score ? ` (${r.score.toFixed(2)})` : "";
								// ponytail: line range — SearchResultItem carries start_line/end_line but
								// the search output never showed them, so a match's location in the file
								// was invisible. `:L42` (single line) or `:L42-50` (range) after the score.
								const startL = r.startLine ?? r.start_line;
								const endL = r.endLine ?? r.end_line;
								const lineTag = (typeof startL === "number" && typeof endL === "number")
									? (startL === endL ? ` :L${startL}` : ` :L${startL}-${endL}`) : "";
								// ponytail: match-type tag — shows how the match was found (text/semantic/
								// ast/hybrid). A hybrid search producing a text match vs a semantic match
								// reads differently; the tag answers "why did this rank here?". Short
								// single-letter labels to keep the line short. Only when matchType present.
								const mt = r.matchType ?? r.match_type ?? "";
								const mtTag = mt ? ` ${mt.charAt(0).toUpperCase()}${mt.slice(1)}` : "";
								const snippet = (r.snippet ?? "").replace(/\s+/g, " ").trim();
								// `  [repo] path score\n      snippet` — the path line prefix
								// is `  [repo] ` (~6 + repo) + score + lineTag + mtTag; cap path so it fits.
								const pathPrefix = `  [${repo}] `;
								const pathBudget = cols !== undefined
									? Math.max(0, cols - pathPrefix.length - (r.score ? score.length : 0) - lineTag.length - mtTag.length)
									: 80;
								// ponytail: slice the PLAIN path, then highlight the query match — a search for a
								// filename or symbol whose name is in the path (e.g. "auth" → src/auth.ts) showed
								// the path raw, so the match in the path was as invisible as the snippet was pre-#519.
								const plainPath = path.length > pathBudget
									? path.slice(0, Math.max(0, pathBudget - 1)) + "…"
									: path;
								const pathStr = highlightQuery(plainPath, rest, (c, t) => ctx.ui.theme.fg(c, t));
								// snippet line prefix is 6 spaces; cap the snippet body.
								const snipBudget = cols !== undefined ? Math.max(0, cols - 6) : 120;
								// ponytail: highlight the query match IN the snippet — the result just dumped the
								// raw snippet, so the user had to eyeball-scan to find where the query landed. Slice
								// the PLAIN snippet first (never a themed string — ANSI splits on slice), THEN wrap
								// query occurrences via highlightQuery (shared w/ the selfcheck). Warning-colored span.
								const plainSnip = snippet.length > snipBudget
									? snippet.slice(0, Math.max(0, snipBudget - 1)) + "…"
									: snippet;
								const highlightedSnip = highlightQuery(plainSnip, rest, (c, t) => ctx.ui.theme.fg(c, t));
								const snip = snippet ? `\n      ${highlightedSnip}` : "";
								// ponytail: AST symbol metadata — SearchResultItem carries symbol_name +
								// symbol_kind (only present on Ast/Hybrid matches). Shows "fn myFunc" or
								// "class Foo" before the snippet, so the user knows WHICH symbol matched
								// without reading the snippet. Only when symbolName is present.
								const symName = r.symbolName ?? r.symbol_name;
								const symKind = r.symbolKind ?? r.symbol_kind;
								// ponytail: highlight the query in the symbol NAME (not the kind — that's a type
								// keyword, not the query). A search for a symbol by name showed ⟦fn myFunc⟧ raw;
								// the match in the name was as invisible as the snippet was pre-#519.
								const symNameHi = symName ? highlightQuery(symName, rest, (c, t) => ctx.ui.theme.fg(c, t)) : "";
								const symTag = (symNameHi && symKind) ? `\n      ⟦${symKind} ${symNameHi}⟧` : (symNameHi ? `\n      ⟦${symNameHi}⟧` : "");
								return `${pathPrefix}${pathStr}${score}${lineTag}${mtTag}${symTag}${snip}`;
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
					// ponytail: F28 — "/uc submti" used to dump help identically to
					// "/uc help", so typos looked like success. Flag unknown input.
					// (Empty input maps to "help" at parse time — only genuine
					// unknown words reach this warning path.)
					ctx.ui.notify(
						[
							...(parts[0] && !SUBCOMMANDS.includes(parts[0])
								? [`Unknown subcommand "${parts[0]}".`, ""]
								: []),
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
							"",
							"In-overlay keys:",
							"  ↑↓/jk nav · Enter expand/detail · / filter · Esc close",
							"  Task list:  c cancel (2×) · p pause · r resume · n next-failed · N prev-failed · y copy id · Y copy err · t subtask tree",
							"  Subtask tree:  R retry · o expand/collapse all · n next-failed · p prev-failed · d task detail · y copy id · Y copy err/result",
							"  / filter matches id, description, status, deps, error/result text, and declared files",
						].join("\n"),
						parts[0] && !SUBCOMMANDS.includes(parts[0]) ? "warning" : "info",
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
