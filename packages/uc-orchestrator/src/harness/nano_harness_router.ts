/**
 * NanoHarness (#50): model-tier and task-aware harness component routing.
 *
 * The 1,800-run factorial study shows that the useful harness surface is much
 * smaller than a generic agent scaffold: structured tool registry and
 * task-specific subagents keep paying off, while lossy compaction, generic
 * subagents and didactic compensation prompts can subtract value. The router
 * therefore keeps the interface layer and drops the compensation layer, and
 * normalises MCP failures into executable repair guidance.
 *
 * Pure deterministic bookkeeping. No model or I/O.
 */

export type ModelTier = "small" | "mid" | "strong";
export type TaskKind =
	| "repo-generation"
	| "bug-fix"
	| "localized-edit"
	| "test-generation";

export type ComponentId =
	| "tool-registry"
	| "task-subagents"
	| "generic-subagents"
	| "lossy-compaction"
	| "compensation-prompts";

export interface ComponentDecision {
	readonly id: ComponentId;
	readonly enabled: boolean;
	readonly reason: string;
}

export interface HarnessPlan {
	readonly tier: ModelTier;
	readonly task: TaskKind;
	readonly components: readonly ComponentDecision[];
	readonly interfaceMode: "structured";
	readonly errorMode: "actionable";
}

/** Published absolute gains (percentage points) from the NanoHarness study. */
export const COMPONENT_GAINS: Readonly<Record<TaskKind, Readonly<Partial<Record<ComponentId, number>>>>> = {
	"repo-generation": {
		"tool-registry": 4.57,
		"task-subagents": 5.91,
	},
	"bug-fix": {
		"tool-registry": 3.54,
		"task-subagents": 4.46,
	},
	"localized-edit": {
		"tool-registry": 2.1,
	},
	"test-generation": {
		"tool-registry": 2.1,
	},
};

/** The canonical component order used in every plan. */
export const COMPONENT_ORDER: readonly ComponentId[] = [
	"tool-registry",
	"task-subagents",
	"generic-subagents",
	"lossy-compaction",
	"compensation-prompts",
];

export class NanoHarnessRouter {
	/** Build a component plan for one model tier and task kind. */
	route(tier: ModelTier, task: TaskKind): HarnessPlan {
		assertTier(tier);
		assertTask(task);
		const small = tier === "small";
		const generation = task === "repo-generation";
		const repairLike = task === "bug-fix" || task === "repo-generation";

		const components: ComponentDecision[] = [
			{
				id: "tool-registry",
				enabled: true,
				reason: "structured tool contracts are the highest-value interface layer",
			},
			{
				id: "task-subagents",
				enabled: generation || task === "bug-fix",
				reason: repairLike
					? "task-specific subagents add a measured gain on repository-level work"
					: "localized edits do not repay subagent coordination",
			},
			{
				id: "generic-subagents",
				enabled: false,
				reason: "generic subagents are a compensation layer with negative factorial gain",
			},
			{
				id: "lossy-compaction",
				enabled: false,
				reason: "lossy compaction drops evidence that the interface layer still needs",
			},
			{
				id: "compensation-prompts",
				enabled: small,
				reason: small
					? "only the smallest models need a bounded didactic fallback"
					: "strong and mid models are harmed by didactic compensation text",
			},
		];

		return {
			tier,
			task,
			components,
			interfaceMode: "structured",
			errorMode: "actionable",
		};
	}

	/** The component decision for one id, or undefined for an unknown id. */
	decision(plan: HarnessPlan, id: ComponentId): ComponentDecision | undefined {
		return plan.components.find((component) => component.id === id);
	}

	/** Stable serialisation for logs and dashboards. */
	toJSON(plan: HarnessPlan): string {
		return JSON.stringify({
			tier: plan.tier,
			task: plan.task,
			interfaceMode: plan.interfaceMode,
			errorMode: plan.errorMode,
			components: plan.components.map((component) => ({
				id: component.id,
				enabled: component.enabled,
				reason: component.reason,
			})),
		});
	}
}

function assertTier(tier: string): asserts tier is ModelTier {
	if (tier !== "small" && tier !== "mid" && tier !== "strong") {
		throw new RangeError(`unknown model tier ${tier}`);
	}
}

function assertTask(task: string): asserts task is TaskKind {
	if (
		task !== "repo-generation" &&
		task !== "bug-fix" &&
		task !== "localized-edit" &&
		task !== "test-generation"
	) {
		throw new RangeError(`unknown task kind ${task}`);
	}
}

export interface McpErrorGuidance {
	readonly code: string;
	readonly message: string;
	readonly action: string;
	readonly executable: boolean;
}

/** Turn an MCP/tool failure into one concrete next action. */
export function guideMcpError(message: string): McpErrorGuidance {
	const lower = message.toLowerCase();
	if (lower.includes("enoent") || lower.includes("no such file")) {
		return {
			code: "ENOENT",
			message,
			action: "verify the path against the workspace root, then retry with an absolute path or create the missing directory",
			executable: true,
		};
	}
	if (lower.includes("eacces") || lower.includes("permission denied")) {
		return {
			code: "EACCES",
			message,
			action: "inspect the file mode and parent directory permissions before retrying; do not rewrite the path",
			executable: true,
		};
	}
	if (lower.includes("timeout") || lower.includes("timed out") || lower.includes("etimedout")) {
		return {
			code: "TIMEOUT",
			message,
			action: "retry with a bounded timeout and a smaller batch, or split the command into independent steps",
			executable: true,
		};
	}
	if (lower.includes("not found") || lower.includes("unknown tool")) {
		return {
			code: "UNKNOWN_TOOL",
			message,
			action: "read the tool registry entry and call the exact advertised name with its required arguments",
			executable: true,
		};
	}
	if (lower.includes("invalid") || lower.includes("schema") || lower.includes("validation")) {
		return {
			code: "INVALID_ARGUMENT",
			message,
			action: "validate the argument object against the tool schema before retrying",
			executable: true,
		};
	}
	return {
		code: "UNCLASSIFIED",
		message,
		action: "inspect the tool schema and the last successful call, then retry with the smallest valid input",
		executable: false,
	};
}
