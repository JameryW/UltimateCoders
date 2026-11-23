/**
 * Agent Lightning v1.0 harness-native RL and Self-Harness helpers (#78).
 *
 * The deployed harness owns the environment interaction loop; the trainer only
 * observes raw LLM request/response pairs. The Self-Harness loop mines failure
 * patterns, proposes minimal harness changes, and verifies them against a
 * regression gate. A trajectory-centroid subset keeps 10% of samples for the
 * regression check. Pure deterministic bookkeeping, no I/O.
 *
 * Flag: `UC_AGENT_LIGHTNING` is read by the Python/worker side; this module is
 * an orchestrator library.
 */

/** Published sample count used by the Agent Lightning v1.0 report. */
export const AGENT_LIGHTNING_SAMPLE_COUNT = 6000;

/** Published Qwen3.5-9B baseline on SWE-bench Verified. */
export const QWEN_BASELINE = 0.418;

/** Published Qwen3.5-9B result after harness-native RL. */
export const QWEN_WITH_RL = 0.564;

/** Published relative gain attributed to Self-Harness. */
export const SELF_HARNESS_RELATIVE_GAIN = 1.32;

/** Fraction of trajectories retained by the centroid subset gate. */
export const TRAJECTORY_SUBSET_FRACTION = 0.1;

export interface LlmRequest {
	readonly id: string;
	readonly prompt: string;
}

export interface LlmResponse {
	readonly requestId: string;
	readonly text: string;
	readonly tokenIds: readonly number[];
}

export type SampleOutcome = "pass" | "fail";

export interface InteractionSample {
	readonly id: string;
	readonly request: LlmRequest;
	readonly response: LlmResponse;
	readonly outcome: SampleOutcome;
	readonly tags: readonly string[];
	readonly tokenCount: number;
	readonly turnCount: number;
	readonly toolCallCount: number;
}

export interface TrainerSample {
	readonly request: LlmRequest;
	readonly response: LlmResponse;
}

/** The trainer-visible projection: exactly the raw request/response pair. */
export function trainerView(sample: InteractionSample): TrainerSample {
	return { request: sample.request, response: sample.response };
}

/** A harness-owned interaction loop that records raw pairs and outcomes. */
export class HarnessLoop {
	private readonly samples: InteractionSample[] = [];

	record(
		request: LlmRequest,
		response: LlmResponse,
		outcome: SampleOutcome,
		tags: readonly string[] = [],
		tokenCount = response.tokenIds.length,
		turnCount = 1,
		toolCallCount = 0,
	): InteractionSample {
		if (!request.id.trim() || !request.prompt.trim()) {
			throw new Error("request id and prompt must not be empty");
		}
		if (response.requestId !== request.id) {
			throw new Error("response requestId must match request id");
		}
		const sample: InteractionSample = {
			id: request.id,
			request,
			response,
			outcome,
			tags: [...tags],
			tokenCount,
			turnCount,
			toolCallCount,
		};
		this.samples.push(sample);
		return sample;
	}

	all(): readonly InteractionSample[] {
		return this.samples;
	}

	trainerSamples(): readonly TrainerSample[] {
		return this.samples.map(trainerView);
	}

	reset(): void {
		this.samples.length = 0;
	}
}

export interface Weakness {
	readonly tag: string;
	readonly failures: number;
	readonly total: number;
	readonly failureRate: number;
}

/** Mine the most frequent failure tags from recorded samples. */
export function mineWeaknesses(
	samples: readonly InteractionSample[],
	minFailures = 1,
): readonly Weakness[] {
	const totals = new Map<string, number>();
	const failures = new Map<string, number>();
	for (const sample of samples) {
		for (const tag of sample.tags) {
			totals.set(tag, (totals.get(tag) ?? 0) + 1);
			if (sample.outcome === "fail") {
				failures.set(tag, (failures.get(tag) ?? 0) + 1);
			}
		}
	}
	const result: Weakness[] = [];
	for (const [tag, total] of totals) {
		const failed = failures.get(tag) ?? 0;
		if (failed >= minFailures) {
			result.push({ tag, failures: failed, total, failureRate: total === 0 ? 0 : failed / total });
		}
	}
	return result.sort((left, right) => {
		if (right.failureRate !== left.failureRate) return right.failureRate - left.failureRate;
		if (right.failures !== left.failures) return right.failures - left.failures;
		return left.tag.localeCompare(right.tag);
	});
}

export interface HarnessProposal {
	readonly id: string;
	readonly title: string;
	readonly change: string;
	readonly targetTags: readonly string[];
	readonly rationale: string;
}

/** Propose a minimal one-line harness change for a weakness. */
export function proposeMinimalPatch(weakness: Weakness): HarnessProposal {
	if (!weakness.tag.trim()) {
		throw new Error("weakness tag must not be empty");
	}
	return {
		id: `patch:${weakness.tag}`,
		title: `Reduce ${weakness.tag} failures`,
		change: `If the trajectory matches ${weakness.tag}, add an explicit verification step before completion.`,
		targetTags: [weakness.tag],
		rationale: `${weakness.failures}/${weakness.total} tagged samples failed.`,
	};
}

export interface RegressionCheck {
	readonly passed: boolean;
	readonly reason: string;
	readonly scoreDelta: number;
}

/** Verify a proposal against held-out baseline and candidate scores. */
export function verifyRegression(
	proposal: HarnessProposal,
	baselineScore: number,
	candidateScore: number,
	tolerance = 0.01,
): RegressionCheck {
	if (!proposal.id.trim()) {
		throw new Error("proposal id must not be empty");
	}
	if (baselineScore < 0 || candidateScore < 0) {
		throw new Error("scores must not be negative");
	}
	const delta = candidateScore - baselineScore;
	if (delta < -Math.abs(tolerance)) {
		return { passed: false, reason: "candidate regressed beyond tolerance", scoreDelta: delta };
	}
	return { passed: true, reason: "regression gate passed", scoreDelta: delta };
}

export interface TrajectoryFeatures {
	readonly id: string;
	readonly tokenCount: number;
	readonly turnCount: number;
	readonly toolCallCount: number;
}

export interface CentroidSubset {
	readonly selected: readonly TrajectoryFeatures[];
	readonly fraction: number;
	readonly medianError: number;
}

function distance(left: TrajectoryFeatures, right: TrajectoryFeatures): number {
	const dt = left.tokenCount - right.tokenCount;
	const dr = left.turnCount - right.turnCount;
	const dc = left.toolCallCount - right.toolCallCount;
	return Math.sqrt(dt * dt + dr * dr + dc * dc);
}

function median(values: readonly number[]): number {
	if (values.length === 0) return 0;
	const sorted = [...values].sort((a, b) => a - b);
	const middle = Math.floor(sorted.length / 2);
	return sorted.length % 2 === 0 ? (sorted[middle - 1] + sorted[middle]) / 2 : sorted[middle];
}

/** Select the trajectory subset closest to the feature centroid. */
export function selectTrajectoryCentroid(
	samples: readonly TrajectoryFeatures[],
	fraction = TRAJECTORY_SUBSET_FRACTION,
): CentroidSubset {
	if (samples.length === 0) {
		return { selected: [], fraction, medianError: 0 };
	}
	if (!(fraction > 0 && fraction <= 1)) {
		throw new Error("fraction must be in (0, 1]");
	}
	const centroid: TrajectoryFeatures = {
		id: "centroid",
		tokenCount: samples.reduce((sum, sample) => sum + sample.tokenCount, 0) / samples.length,
		turnCount: samples.reduce((sum, sample) => sum + sample.turnCount, 0) / samples.length,
		toolCallCount: samples.reduce((sum, sample) => sum + sample.toolCallCount, 0) / samples.length,
	};
	const ranked = [...samples].sort((left, right) => {
		const d = distance(left, centroid) - distance(right, centroid);
		return d !== 0 ? d : left.id.localeCompare(right.id);
	});
	const count = Math.max(1, Math.ceil(samples.length * fraction));
	const selected = ranked.slice(0, count);
	return {
		selected,
		fraction,
		medianError: median(selected.map((sample) => distance(sample, centroid))),
	};
}

export interface EvolutionReport {
	readonly weaknesses: readonly Weakness[];
	readonly proposals: readonly HarnessProposal[];
	readonly accepted: readonly string[];
	readonly subset: CentroidSubset;
}

/** One deterministic Self-Harness evolution pass. */
export class SelfHarnessEvolver {
	evolve(
		samples: readonly InteractionSample[],
		regressionScores: ReadonlyMap<string, { baseline: number; candidate: number }> = new Map(),
	): EvolutionReport {
		const weaknesses = mineWeaknesses(samples);
		const proposals = weaknesses.map(proposeMinimalPatch);
		const accepted: string[] = [];
		for (const proposal of proposals) {
			const score = regressionScores.get(proposal.id);
			if (score && verifyRegression(proposal, score.baseline, score.candidate).passed) {
				accepted.push(proposal.id);
			}
		}
		return {
			weaknesses,
			proposals,
			accepted,
			subset: selectTrajectoryCentroid(samples),
		};
	}
}
