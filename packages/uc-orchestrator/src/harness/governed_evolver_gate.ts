/**
 * Governed self-evolution gate for the harness.
 *
 * Implements ledger item #62 (A-Evolve, arXiv:2605.30621; Poisoning
 * Self-Evolving Agents, arXiv:2611.10542; G-CAST, arXiv:2611.17814).
 * Harness-updating is cheap; harness-benefit and safety are not. Every
 * proposal must branch from an immutable parent snapshot, keep the historical
 * regression suite green, improve the new-task suite, and pass a deterministic
 * poison/trigger scan before it can replace the parent.
 */

export type ProposalKind = "prompt" | "skill" | "harness_code";

export interface HarnessProposal {
	readonly id: string;
	readonly kind: ProposalKind;
	readonly parentHash: string;
	readonly newSnapshotHash: string;
	readonly patch: string;
	readonly newTasks: readonly string[];
	readonly triggerTerms?: readonly string[];
}

export interface EvaluationCase {
	readonly task: string;
	readonly passed: boolean;
}

export interface ProposalEvaluation {
	readonly proposalId: string;
	readonly parentHash: string;
	readonly regressionPassed: number;
	readonly regressionTotal: number;
	readonly newTaskPassed: number;
	readonly newTaskTotal: number;
	readonly poisonedTerms: readonly string[];
}

export interface GateDecision {
	readonly accepted: boolean;
	readonly reasons: readonly string[];
}

export interface GateOptions {
	readonly minRegressionPassRate?: number;
	readonly minNewTaskPassRate?: number;
	readonly minNewTasks?: number;
}

const DEFAULT_POISON_TERMS = [
	"ignore previous instructions",
	"exfiltrate",
	"send the token",
	"disable the verifier",
	"skip all tests",
	"approve without review",
];

export class GovernedEvolverGate {
	private snapshot: string;
	private readonly options: Required<GateOptions>;

	constructor(parentSnapshot: string, options: GateOptions = {}) {
		if (!parentSnapshot.trim()) {
			throw new Error("parent snapshot must not be empty");
		}
		this.snapshot = parentSnapshot;
		this.options = {
			minRegressionPassRate: options.minRegressionPassRate ?? 1,
			minNewTaskPassRate: options.minNewTaskPassRate ?? 0.8,
			minNewTasks: options.minNewTasks ?? 1,
		};
	}

	currentSnapshot(): string {
		return this.snapshot;
	}

	evaluate(
		proposal: HarnessProposal,
		regressionCases: readonly EvaluationCase[],
		newTaskCases: readonly EvaluationCase[],
	): ProposalEvaluation {
		const polish = (cases: readonly EvaluationCase[]) => ({
			passed: cases.filter((entry) => entry.passed).length,
			total: cases.length,
		});
		const regression = polish(regressionCases);
		const newTasks = polish(newTaskCases);
		const poisonedTerms = this.poisonedTerms(proposal);
		return {
			proposalId: proposal.id,
			parentHash: proposal.parentHash,
			regressionPassed: regression.passed,
			regressionTotal: regression.total,
			newTaskPassed: newTasks.passed,
			newTaskTotal: newTasks.total,
			poisonedTerms,
		};
	}

	promote(
		proposal: HarnessProposal,
		evaluation: ProposalEvaluation,
	): GateDecision {
		const reasons: string[] = [];
		if (!proposal.id.trim()) {
			reasons.push("proposal id is empty");
		}
		if (proposal.parentHash !== this.snapshot) {
			reasons.push("proposal does not branch from the current parent snapshot");
		}
		if (!proposal.newSnapshotHash.trim()) {
			reasons.push("proposal has no new snapshot hash");
		}
		if (evaluation.proposalId !== proposal.id) {
			reasons.push("evaluation does not belong to this proposal");
		}
		if (evaluation.parentHash !== proposal.parentHash) {
			reasons.push("evaluation parent hash does not match proposal");
		}
		if (evaluation.regressionTotal === 0) {
			reasons.push("regression suite is empty");
		} else if (
			evaluation.regressionPassed / evaluation.regressionTotal <
			this.options.minRegressionPassRate
		) {
			reasons.push("historical regression suite is not fully green");
		}
		if (evaluation.newTaskTotal < this.options.minNewTasks) {
			reasons.push("not enough new tasks were evaluated");
		} else if (
			evaluation.newTaskPassed / evaluation.newTaskTotal <
			this.options.minNewTaskPassRate
		) {
			reasons.push("new-task pass rate is below the gate");
		}
		if (evaluation.poisonedTerms.length > 0) {
			reasons.push(
				`poison trigger terms found: ${evaluation.poisonedTerms.join(", ")}`,
			);
		}
		if (reasons.length > 0) {
			return { accepted: false, reasons };
		}
		this.snapshot = proposal.newSnapshotHash;
		return { accepted: true, reasons: [] };
	}

	private poisonedTerms(proposal: HarnessProposal): readonly string[] {
		const haystack = [
			proposal.patch,
			...proposal.newTasks,
			...(proposal.triggerTerms ?? []),
		]
			.join("\n")
			.toLowerCase();
		return DEFAULT_POISON_TERMS.filter((term) => haystack.includes(term));
	}
}
