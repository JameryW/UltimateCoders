/**
 * RiseHarness: turn flipping-task traces into harness evolution proposals.
 *
 * Implements ledger item #61 (RiseHarness, arXiv:2610.27435 and Harness
 * Handbook, arXiv:2607.13285). A flipping task succeeds and fails under the
 * same model and harness configuration. Aligning the visible agent trace with
 * the internal harness trace localises the runtime responsibility that differs,
 * so the fix can be a mechanism port rather than another model prompt.
 *
 * The evolver is deterministic and never edits a source file or calls a model.
 */

export type HarnessResponsibility =
	| "command_execution"
	| "feedback_truncation"
	| "context_compression"
	| "completion_gate";

export interface HarnessHandbookEntry {
	readonly responsibility: HarnessResponsibility;
	readonly sourceFile: string;
	readonly functionName: string;
	readonly notes: string;
}

export class HarnessHandbook {
	private readonly entries: HarnessHandbookEntry[] = [];

	register(entry: HarnessHandbookEntry): void {
		if (!entry.sourceFile.trim() || !entry.functionName.trim()) {
			throw new Error("handbook entry needs a source file and function");
		}
		if (
			this.entries.some(
				(existing) => existing.responsibility === entry.responsibility,
			)
		) {
			throw new Error(
				`duplicate handbook responsibility: ${entry.responsibility}`,
			);
		}
		this.entries.push(entry);
	}

	locate(responsibility: HarnessResponsibility): HarnessHandbookEntry | undefined {
		return this.entries.find(
			(entry) => entry.responsibility === responsibility,
		);
	}

	all(): readonly HarnessHandbookEntry[] {
		return this.entries;
	}

	render(): string {
		const lines = ["# HARNESS_HANDBOOK.yaml"];
		for (const entry of this.entries) {
			lines.push(
				`- responsibility: ${entry.responsibility}`,
				`  source_file: ${entry.sourceFile}`,
				`  function: ${entry.functionName}`,
				`  notes: ${entry.notes}`,
			);
		}
		return lines.join("\n");
	}
}

export interface TraceStep {
	readonly step: number;
	readonly agentAction?: string;
	readonly harnessEvent?: string;
}

export interface HarnessAttempt {
	readonly attemptId: string;
	readonly model: string;
	readonly configHash: string;
	readonly success: boolean;
	readonly agentTrace: readonly TraceStep[];
	readonly harnessTrace: readonly TraceStep[];
}

export interface FlippingTask {
	readonly taskId: string;
	readonly attempts: readonly HarnessAttempt[];
}

export function findFlippingTasks(
	tasks: readonly FlippingTask[],
): readonly FlippingTask[] {
	return tasks.filter((task) => {
		const groups = new Map<string, { success: boolean; failure: boolean }>();
		for (const attempt of task.attempts) {
			const key = `${attempt.model}::${attempt.configHash}`;
			const group = groups.get(key) ?? { success: false, failure: false };
			group.success ||= attempt.success;
			group.failure ||= !attempt.success;
			groups.set(key, group);
		}
		return [...groups.values()].some(
			(group) => group.success && group.failure,
		);
	});
}

export interface TraceAlignment {
	readonly step: number;
	readonly agentAction: string;
	readonly harnessEvent: string;
	readonly suspicious: boolean;
}

const SUSPICIOUS_EVENTS = new Set([
	"timeout",
	"truncated",
	"empty_observation",
	"compaction_missing_state",
	"false_completion",
	"gate_closed",
]);

export function alignTraces(
	agentTrace: readonly TraceStep[],
	harnessTrace: readonly TraceStep[],
): readonly TraceAlignment[] {
	const harnessByStep = new Map(
		harnessTrace.map((step) => [step.step, step.harnessEvent ?? ""]),
	);
	return agentTrace.map((step) => {
		const harnessEvent = harnessByStep.get(step.step) ?? "";
		return {
			step: step.step,
			agentAction: step.agentAction ?? "",
			harnessEvent,
			suspicious:
				!harnessEvent || SUSPICIOUS_EVENTS.has(harnessEvent),
		};
	});
}

export interface EvolutionProposal {
	readonly responsibility: HarnessResponsibility;
	readonly sourceFile: string;
	readonly functionName: string;
	readonly evidence: string;
	readonly mechanism: string;
}

export interface EvolutionReport {
	readonly flippingTasks: readonly FlippingTask[];
	readonly alignments: ReadonlyMap<string, readonly TraceAlignment[]>;
	readonly proposals: readonly EvolutionProposal[];
}

const RESPONSIBILITY_ORDER: readonly HarnessResponsibility[] = [
	"command_execution",
	"feedback_truncation",
	"context_compression",
	"completion_gate",
];

const RESPONSIBILITY_MECHANISMS: Record<HarnessResponsibility, string> = {
	command_execution:
		"Retry a timed-out tool in a fresh process group and preserve the exit status.",
	feedback_truncation:
		"Preserve the failing assertion and the final command when truncating output.",
	context_compression:
		"Write a working-state card before compacting and reference it after the rewrite.",
	completion_gate:
		"Require a test execution after the final edit before accepting completion.",
};

export class RiseHarnessEvolver {
	constructor(private readonly handbook: HarnessHandbook) {}

	diagnose(tasks: readonly FlippingTask[]): EvolutionReport {
		const flipping = findFlippingTasks(tasks);
		const alignments = new Map<string, readonly TraceAlignment[]>();
		const evidenceByResponsibility = new Map<HarnessResponsibility, string[]>();

		for (const task of flipping) {
			for (const attempt of task.attempts) {
				if (attempt.success) {
					continue;
				}
				const aligned = alignTraces(attempt.agentTrace, attempt.harnessTrace);
				alignments.set(attempt.attemptId, aligned);
				for (const step of aligned) {
					if (!step.suspicious) {
						continue;
					}
					const responsibility = responsibilityFor(step.harnessEvent);
					const evidence = `${task.taskId}/${attempt.attemptId} step ${step.step}: ${step.harnessEvent || "missing harness event"}`;
					const existing =
						evidenceByResponsibility.get(responsibility) ?? [];
					existing.push(evidence);
					evidenceByResponsibility.set(responsibility, existing);
				}
			}
		}

		const proposals: EvolutionProposal[] = [];
		for (const responsibility of RESPONSIBILITY_ORDER) {
			const entry = this.handbook.locate(responsibility);
			const evidence = evidenceByResponsibility.get(responsibility);
			if (!entry || !evidence || evidence.length === 0) {
				continue;
			}
			proposals.push({
				responsibility,
				sourceFile: entry.sourceFile,
				functionName: entry.functionName,
				evidence: evidence.slice(0, 4).join(" | "),
				mechanism: RESPONSIBILITY_MECHANISMS[responsibility],
			});
		}

		return { flippingTasks: flipping, alignments, proposals };
	}

	port(
		responsibility: HarnessResponsibility,
		externalFramework: string,
	): EvolutionProposal {
		const entry = this.handbook.locate(responsibility);
		if (!entry) {
			throw new Error(`no handbook entry for ${responsibility}`);
		}
		if (!externalFramework.trim()) {
			throw new Error("external framework must not be empty");
		}
		return {
			responsibility,
			sourceFile: entry.sourceFile,
			functionName: entry.functionName,
			evidence: `external mechanism: ${externalFramework.trim()}`,
			mechanism: RESPONSIBILITY_MECHANISMS[responsibility],
		};
	}
}

function responsibilityFor(
	event: string,
): HarnessResponsibility {
	if (event === "timeout" || event === "empty_observation") {
		return "command_execution";
	}
	if (event === "truncated") {
		return "feedback_truncation";
	}
	if (event === "compaction_missing_state") {
		return "context_compression";
	}
	return "completion_gate";
}
