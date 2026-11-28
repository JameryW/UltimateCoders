/**
 * XRepoSkill + Continual Harness Evolution (#47).
 *
 * Two ledger mechanisms are combined here:
 *
 * - Harness evolution is routed per task family (repair, test generation,
 *   feature work, refactoring). A single global harness forgets earlier task
 *   families; isolated profiles stop the cross-family interference.
 * - Skills are distilled from the first divergence point between a successful
 *   and a failed trace, and kept only when a zero-LLM executable predicate
 *   validates across multiple independent repositories with a significant
 *   positive gain.
 *
 * Pure deterministic bookkeeping. No I/O, network, or model call.
 */

export type TaskFamily = "repair" | "test-gen" | "feature" | "refactor";

export interface HarnessProfile {
	readonly family: TaskFamily;
	readonly verification: string;
	readonly workflow: readonly string[];
	readonly budget: number;
}

export const DEFAULT_HARNESS_PROFILES: Readonly<Record<TaskFamily, HarnessProfile>> = {
	repair: {
		family: "repair",
		verification: "failing-test-first",
		workflow: ["reproduce", "localize", "patch", "regression"],
		budget: 30,
	},
	"test-gen": {
		family: "test-gen",
		verification: "coverage-and-mutation",
		workflow: ["inspect", "author-test", "baseline", "harden"],
		budget: 20,
	},
	feature: {
		family: "feature",
		verification: "spec-and-acceptance",
		workflow: ["perceive", "plan", "implement", "verify"],
		budget: 40,
	},
	refactor: {
		family: "refactor",
		verification: "behavior-preserving",
		workflow: ["snapshot", "rewrite", "regression", "rollback"],
		budget: 25,
	},
};

function cloneProfile(profile: HarnessProfile): HarnessProfile {
	return {
		family: profile.family,
		verification: profile.verification,
		workflow: [...profile.workflow],
		budget: profile.budget,
	};
}

/** Route harness configuration by task family instead of one global harness. */
export class TaskFamilyHarnessRouter {
	private readonly profiles = new Map<TaskFamily, HarnessProfile>();

	constructor(profiles: Partial<Record<TaskFamily, HarnessProfile>> = {}) {
		for (const family of Object.keys(DEFAULT_HARNESS_PROFILES) as TaskFamily[]) {
			const profile = profiles[family] ?? DEFAULT_HARNESS_PROFILES[family];
			this.profiles.set(family, cloneProfile(profile));
		}
	}

	/** Resolve a defensive copy of one family's harness profile. */
	resolve(family: TaskFamily): HarnessProfile {
		const profile = this.profiles.get(family);
		if (!profile) {
			throw new RangeError(`unknown task family: ${String(family)}`);
		}
		return cloneProfile(profile);
	}

	/** Replace one family's profile without touching the other families. */
	register(profile: HarnessProfile): void {
		if (!profile.family || !profile.verification.trim()) {
			throw new RangeError("harness profile requires a family and verification");
		}
		if (!Number.isFinite(profile.budget) || profile.budget <= 0) {
			throw new RangeError("harness budget must be a positive finite number");
		}
		this.profiles.set(profile.family, cloneProfile(profile));
	}

	families(): TaskFamily[] {
		return [...this.profiles.keys()].sort();
	}
}

export type PredicateField = "changedFiles" | "testPassRate" | "errorCount" | "tokenCount";
export type PredicateOperator = ">" | ">=" | "<" | "<=" | "==" | "!=";

/** A zero-LLM executable predicate attached to a distilled skill. */
export interface ExecutablePredicate {
	readonly field: PredicateField;
	readonly op: PredicateOperator;
	readonly value: number;
}

export interface SkillEvidence {
	readonly changedFiles?: number;
	readonly testPassRate?: number;
	readonly errorCount?: number;
	readonly tokenCount?: number;
}

/** Evaluate a predicate against an observed evidence bag. */
export function evaluatePredicate(
	predicate: ExecutablePredicate,
	evidence: SkillEvidence,
): boolean {
	const observed = evidence[predicate.field];
	if (observed === undefined) return false;
	switch (predicate.op) {
		case ">":
			return observed > predicate.value;
		case ">=":
			return observed >= predicate.value;
		case "<":
			return observed < predicate.value;
		case "<=":
			return observed <= predicate.value;
		case "==":
			return observed === predicate.value;
		case "!=":
			return observed !== predicate.value;
		default:
			return false;
	}
}

export interface SkillTrace {
	readonly taskId: string;
	readonly family: TaskFamily;
	readonly repo: string;
	readonly steps: readonly string[];
	readonly outcome: "success" | "failure";
}

export interface ForkPoint {
	readonly taskId: string;
	readonly step: number;
	readonly before: string;
	readonly after: string;
	readonly succeeded: boolean;
}

/** Locate the first action where a successful and a failed trace diverged. */
export function firstDivergence(
	success: SkillTrace,
	failure: SkillTrace,
): ForkPoint | null {
	if (success.taskId !== failure.taskId) {
		throw new RangeError("firstDivergence requires traces for the same task");
	}
	if (success.outcome !== "success" || failure.outcome !== "failure") {
		throw new RangeError("firstDivergence expects one success and one failure");
	}
	const limit = Math.min(success.steps.length, failure.steps.length);
	for (let step = 0; step < limit; step++) {
		if (success.steps[step] !== failure.steps[step]) {
			return {
				taskId: success.taskId,
				step,
				before: failure.steps[step],
				after: success.steps[step],
				succeeded: true,
			};
		}
	}
	if (success.steps.length !== failure.steps.length) {
		return {
			taskId: success.taskId,
			step: limit,
			before: failure.steps[limit] ?? "<end>",
			after: success.steps[limit] ?? "<end>",
			succeeded: true,
		};
	}
	return null;
}

export interface SkillCandidate {
	readonly id: string;
	readonly family: TaskFamily;
	readonly statement: string;
	readonly predicate: ExecutablePredicate;
	readonly fork: ForkPoint;
}

export interface SkillOutcome {
	readonly candidateId: string;
	readonly repo: string;
	readonly gain: number;
}

export interface SkillEvaluation {
	readonly candidateId: string;
	readonly repos: readonly string[];
	readonly meanGain: number;
	readonly zScore: number;
	readonly keep: boolean;
}

function evidencePredicate(family: TaskFamily): ExecutablePredicate {
	switch (family) {
		case "repair":
			return { field: "errorCount", op: ">=", value: 1 };
		case "test-gen":
			return { field: "testPassRate", op: "<", value: 0.5 };
		case "feature":
			return { field: "changedFiles", op: ">=", value: 1 };
		case "refactor":
			return { field: "testPassRate", op: ">=", value: 0.5 };
	}
}

function slug(value: string): string {
	return value
		.trim()
		.toLowerCase()
		.replace(/[^a-z0-9]+/g, "-")
		.replace(/^-+|-+$/g, "")
		.slice(0, 32);
}

/** Distil cross-repository skills from success/failure trace pairs. */
export class XRepoSkillDistiller {
	private readonly traces: SkillTrace[] = [];
	private readonly maxTraces: number;

	constructor(maxTraces = 512) {
		if (!Number.isInteger(maxTraces) || maxTraces < 2) {
			throw new RangeError("maxTraces must be an integer >= 2");
		}
		this.maxTraces = maxTraces;
	}

	record(trace: SkillTrace): void {
		if (!trace.taskId.trim() || !trace.repo.trim()) {
			throw new RangeError("trace requires taskId and repo");
		}
		if (trace.steps.length === 0) {
			throw new RangeError("trace requires at least one step");
		}
		this.traces.push({
			taskId: trace.taskId,
			family: trace.family,
			repo: trace.repo,
			steps: [...trace.steps],
			outcome: trace.outcome,
		});
		if (this.traces.length > this.maxTraces) {
			this.traces.splice(0, this.traces.length - this.maxTraces);
		}
	}

	traceCount(): number {
		return this.traces.length;
	}

	/** One candidate per distinct first-divergence point, stable by id. */
	candidates(): SkillCandidate[] {
		const byTask = new Map<string, { success?: SkillTrace; failure?: SkillTrace }>();
		for (const trace of this.traces) {
			const pair = byTask.get(trace.taskId) ?? {};
			if (trace.outcome === "success" && !pair.success) pair.success = trace;
			if (trace.outcome === "failure" && !pair.failure) pair.failure = trace;
			byTask.set(trace.taskId, pair);
		}
		const candidates = new Map<string, SkillCandidate>();
		for (const pair of byTask.values()) {
			if (!pair.success || !pair.failure) continue;
			const fork = firstDivergence(pair.success, pair.failure);
			if (!fork) continue;
			const id = `skill-${fork.succeeded ? "ok" : "fail"}-${pair.success.family}-${fork.step}-${slug(fork.after)}`;
			if (candidates.has(id)) continue;
			candidates.set(id, {
				id,
				family: pair.success.family,
				statement: `At step ${fork.step} of ${pair.success.family}, prefer \`${fork.after}\` over \`${fork.before}\`.`,
				predicate: evidencePredicate(pair.success.family),
				fork,
			});
		}
		return [...candidates.values()].sort((left, right) => left.id.localeCompare(right.id));
	}

	/** Keep only skills with positive cross-repository gain and z-score. */
	evaluate(
		candidates: readonly SkillCandidate[],
		outcomes: readonly SkillOutcome[],
		minRepos = 2,
		minZ = 1.0,
	): SkillEvaluation[] {
		const grouped = new Map<string, SkillOutcome[]>();
		for (const outcome of outcomes) {
			const bucket = grouped.get(outcome.candidateId) ?? [];
			bucket.push(outcome);
			grouped.set(outcome.candidateId, bucket);
		}
		return candidates.map((candidate) => {
			const observed = grouped.get(candidate.id) ?? [];
			const repos = [...new Set(observed.map((outcome) => outcome.repo))].sort();
			const gains = observed.map((outcome) => outcome.gain);
			const meanGain = gains.length === 0 ? 0 : gains.reduce((a, b) => a + b, 0) / gains.length;
			let variance = 0;
			for (const gain of gains) variance += (gain - meanGain) ** 2;
			variance = gains.length > 1 ? variance / (gains.length - 1) : 0;
			const std = Math.sqrt(variance);
			const zScore = std < 1e-9 ? (meanGain > 0 ? 999 : 0) : meanGain / std;
			const keep =
				repos.length >= minRepos &&
				meanGain > 0 &&
				Number.isFinite(meanGain) &&
				zScore >= minZ;
			return { candidateId: candidate.id, repos, meanGain, zScore, keep };
		});
	}
}
