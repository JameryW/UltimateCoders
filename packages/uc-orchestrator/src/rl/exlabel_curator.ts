/**
 * ExLabel (#41) — self-evolving task-clarity and test-coverage curation.
 *
 * The experience bank learns ambiguity labels from human/Oracle adjudications.
 * A trajectory is only exported for SFT/RL when its task is well specified and
 * its F2P/P2P tests are adequately covered. The curator is deterministic and
 * side-effect free: it performs no model call and does not touch disk.
 *
 * Ledger item: #41 (arXiv:2610.39417). The ledger's original path ended in
 * `.py` under the TypeScript package; the implementation lives beside the other
 * RL data-planning modules as `exlabel_curator.ts`.
 */

/** One adjudicated example distilled into the experience bank. */
export interface ExperienceRecord {
	/** Stable record id, used only for validation/diagnostics. */
	id: string;
	/** Ambiguity labels observed in the task statement. */
	ambiguityLabels: readonly string[];
	/** Human/Oracle clarity in [0, 1]. */
	clarity: number;
	/** Observed F2P coverage in [0, 1]. */
	f2pCoverage: number;
	/** Observed P2P coverage in [0, 1]. */
	p2pCoverage: number;
}

/** Test inventory for one candidate task. */
export interface TestCoverageInput {
	f2pTotal: number;
	f2pCovered: number;
	p2pTotal: number;
	p2pCovered: number;
}

/** Task-clarity score produced by the learned bank. */
export interface TaskClarityScore {
	score: number;
	labels: string[];
	sourceFileMention: boolean;
}

/** Test-coverage score for one candidate task. */
export interface TestCoverageScore {
	score: number;
	f2pCoverage: number;
	p2pCoverage: number;
	missingF2P: number;
	missingP2P: number;
}

/** Final keep/drop verdict for one trajectory. */
export interface CurationVerdict {
	keep: boolean;
	clarity: TaskClarityScore;
	coverage: TestCoverageScore;
	reasons: string[];
}

/** Curator thresholds and bounds. */
export interface CuratorOptions {
	clarityThreshold?: number;
	coverageThreshold?: number;
	labelPenaltyCap?: number;
}

const DEFAULT_OPTIONS: Required<CuratorOptions> = {
	clarityThreshold: 0.6,
	coverageThreshold: 0.6,
	labelPenaltyCap: 0.6,
};

const SOURCE_FILE = /(?:^|[\s`'"(])(?:[\w.-]+\/)+[\w.-]+\.(?:py|ts|tsx|js|jsx|rs|go|java|c|cc|cpp|h|hpp|md)(?:$|[\s`'")])/i;

function clamp01(value: number): number {
	if (!Number.isFinite(value)) {
		throw new RangeError("value must be finite");
	}
	return Math.max(0, Math.min(1, value));
}

function assertNonNegativeInteger(value: number, name: string): void {
	if (!Number.isInteger(value) || value < 0) {
		throw new RangeError(`${name} must be a non-negative integer`);
	}
}

function normaliseLabels(labels: readonly string[]): string[] {
	const seen: string[] = [];
	for (const label of labels) {
		const trimmed = label.trim().toLowerCase();
		if (trimmed.length === 0) continue;
		if (!seen.includes(trimmed)) seen.push(trimmed);
	}
	return seen;
}

/**
 * Learns label severity from adjudications and scores candidate trajectories.
 */
export class ExLabelCurator {
	private readonly options: Required<CuratorOptions>;
	private readonly weights = new Map<string, number>();
	private learnedRecords = 0;

	constructor(options: CuratorOptions = {}) {
		this.options = { ...DEFAULT_OPTIONS, ...options };
		if (this.options.clarityThreshold < 0 || this.options.clarityThreshold > 1) {
			throw new RangeError("clarityThreshold must be in [0, 1]");
		}
		if (this.options.coverageThreshold < 0 || this.options.coverageThreshold > 1) {
			throw new RangeError("coverageThreshold must be in [0, 1]");
		}
		if (this.options.labelPenaltyCap < 0 || this.options.labelPenaltyCap > 1) {
			throw new RangeError("labelPenaltyCap must be in [0, 1]");
		}
	}

	/** Number of adjudicated records consumed by the bank. */
	get bankSize(): number {
		return this.learnedRecords;
	}

	/** Learned label weights, sorted for deterministic reporting. */
	labels(): string[] {
		return [...this.weights.keys()].sort();
	}

	/** Weight of one label; unknown labels have zero weight. */
	labelWeight(label: string): number {
		return this.weights.get(label.trim().toLowerCase()) ?? 0;
	}

	/**
	 * Learn from one adjudication. A label seen in a low-clarity task gains
	 * more penalty weight; a label seen only in clear tasks gains little.
	 */
	learn(record: ExperienceRecord): void {
		if (record.id.trim().length === 0) {
			throw new TypeError("record.id must be a non-empty string");
		}
		const clarity = clamp01(record.clarity);
		clamp01(record.f2pCoverage);
		clamp01(record.p2pCoverage);
		const labels = normaliseLabels(record.ambiguityLabels);
		if (labels.length === 0) {
			throw new RangeError("record must contain at least one ambiguity label");
		}
		for (const label of labels) {
			const next = (this.weights.get(label) ?? 0) + (1 - clarity);
			this.weights.set(label, next);
		}
		this.learnedRecords += 1;
	}

	/** Score the task statement against the learned ambiguity labels. */
	evaluateClarity(task: string): TaskClarityScore {
		if (task.trim().length === 0) {
			throw new TypeError("task must be a non-empty string");
		}
		const lower = task.toLowerCase();
		const labels = this.labels().filter((label) => lower.includes(label));
		const penalty = Math.min(
			this.options.labelPenaltyCap,
			labels.reduce((sum, label) => sum + this.labelWeight(label), 0),
		);
		const sourceFileMention = SOURCE_FILE.test(task);
		const score = Math.max(0, 1 - penalty - (sourceFileMention ? 0 : 0.15));
		return { score, labels, sourceFileMention };
	}

	/** Compute F2P/P2P coverage. A task with no P2P tests is not penalised. */
	evaluateCoverage(tests: TestCoverageInput): TestCoverageScore {
		for (const [name, value] of [
			["f2pTotal", tests.f2pTotal],
			["f2pCovered", tests.f2pCovered],
			["p2pTotal", tests.p2pTotal],
			["p2pCovered", tests.p2pCovered],
		] as const) {
			assertNonNegativeInteger(value, name);
		}
		if (tests.f2pCovered > tests.f2pTotal) {
			throw new RangeError("f2pCovered cannot exceed f2pTotal");
		}
		if (tests.p2pCovered > tests.p2pTotal) {
			throw new RangeError("p2pCovered cannot exceed p2pTotal");
		}
		const f2pCoverage =
			tests.f2pTotal === 0 ? 0 : tests.f2pCovered / tests.f2pTotal;
		const p2pCoverage =
			tests.p2pTotal === 0 ? 1 : tests.p2pCovered / tests.p2pTotal;
		return {
			score: 0.7 * f2pCoverage + 0.3 * p2pCoverage,
			f2pCoverage,
			p2pCoverage,
			missingF2P: tests.f2pTotal - tests.f2pCovered,
			missingP2P: tests.p2pTotal - tests.p2pCovered,
		};
	}

	/** Keep a trajectory only when both axes clear their thresholds. */
	curate(task: string, tests: TestCoverageInput): CurationVerdict {
		const clarity = this.evaluateClarity(task);
		const coverage = this.evaluateCoverage(tests);
		const reasons: string[] = [];
		if (clarity.score < this.options.clarityThreshold) {
			reasons.push("low_task_clarity");
		}
		if (tests.f2pTotal === 0) {
			reasons.push("missing_f2p_tests");
		} else if (coverage.f2pCoverage < this.options.coverageThreshold) {
			reasons.push("insufficient_f2p_coverage");
		}
		if (coverage.p2pCoverage < this.options.coverageThreshold) {
			reasons.push("insufficient_p2p_coverage");
		}
		if (coverage.score < this.options.coverageThreshold) {
			reasons.push("low_test_coverage");
		}
		return { keep: reasons.length === 0, clarity, coverage, reasons };
	}

	/** Filter a batch while preserving input order. */
	curateBatch(
		candidates: readonly { task: string; tests: TestCoverageInput }[],
	): CurationVerdict[] {
		return candidates.map((candidate) => this.curate(candidate.task, candidate.tests));
	}

	/** Bounded diagnostic snapshot. */
	toJSON(): Record<string, unknown> {
		return {
			bankSize: this.learnedRecords,
			labels: this.labels(),
			thresholds: {
				clarity: this.options.clarityThreshold,
				coverage: this.options.coverageThreshold,
			},
		};
	}
}
