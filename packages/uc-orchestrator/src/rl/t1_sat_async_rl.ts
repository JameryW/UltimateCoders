/**
 * T1 token-exact terminal RL and SAT staleness-adaptive trust regions.
 *
 * Implements ledger item #64 (T1, arXiv:2609.11042; SAT, arXiv:2607.18722).
 * The batch keeps the sampler's original token ids so a 300-turn terminal
 * trajectory is never silently re-tokenised; turn-boundary drift is repaired
 * in place and MoE routes are replayed from the sampling pass. SAT then
 * shrinks only the outward PPO/GSPO clipping bound for the stale tail.
 */

export interface SampledToken {
	readonly id: number;
	readonly turn: number;
	readonly logprob: number;
	readonly route?: string;
}

export interface TitoBatch {
	readonly ids: readonly number[];
	readonly turnIds: readonly number[];
	readonly lossMask: readonly boolean[];
	readonly driftRepairs: number;
}

export function buildTitoBatch(
	tokens: readonly SampledToken[],
	mask: readonly boolean[] = [],
): TitoBatch {
	if (tokens.length === 0) {
		throw new Error("a TITO batch needs at least one token");
	}
	if (mask.length > 0 && mask.length !== tokens.length) {
		throw new Error("loss mask must align with the token sequence");
	}
	const ids: number[] = [];
	const turnIds: number[] = [];
	let previousTurn = -1;
	let driftRepairs = 0;
	for (const [index, token] of tokens.entries()) {
		if (!Number.isInteger(token.id) || token.id < 0) {
			throw new Error(`invalid token id at ${index}`);
		}
		if (!Number.isInteger(token.turn) || token.turn < 0) {
			throw new Error(`invalid turn at ${index}`);
		}
		let turn = token.turn;
		if (turn < previousTurn) {
			turn = previousTurn;
			driftRepairs += 1;
		}
		previousTurn = turn;
		ids.push(token.id);
		turnIds.push(turn);
	}
	return {
		ids,
		turnIds,
		lossMask: mask.length > 0 ? [...mask] : tokens.map(() => true),
		driftRepairs,
	};
}

export interface RouteReplay {
	readonly routes: readonly string[];
	readonly replayed: number;
}

export function replayMoERoutes(tokens: readonly SampledToken[]): RouteReplay {
	const firstRouteByTurn = new Map<number, string>();
	const routes: string[] = [];
	let replayed = 0;
	for (const token of tokens) {
		const route = token.route ?? "default";
		const first = firstRouteByTurn.get(token.turn);
		if (first === undefined) {
			firstRouteByTurn.set(token.turn, route);
			routes.push(route);
			continue;
		}
		routes.push(first);
		if (first !== route) {
			replayed += 1;
		}
	}
	return { routes, replayed };
}

export interface ClipRange {
	readonly negative: number;
	readonly positive: number;
	readonly threshold: number;
}

export function quantile(values: readonly number[], q: number): number {
	if (values.length === 0) {
		throw new Error("quantile needs at least one value");
	}
	if (q < 0 || q > 1) {
		throw new Error("quantile must be in [0, 1]");
	}
	const sorted = [...values].sort((left, right) => left - right);
	const rank = Math.ceil(q * sorted.length) - 1;
	const index = Math.min(sorted.length - 1, Math.max(0, rank));
	return sorted[index];
}

export function stalenessFromLogRatio(logRatio: number): number {
	if (!Number.isFinite(logRatio)) {
		throw new Error("log ratio must be finite");
	}
	return Math.abs(logRatio);
}

export class StalenessAdaptiveTrustRegion {
	constructor(
		private readonly baseClip = 0.2,
		private readonly quantileThreshold = 0.9,
		private readonly shrinkFactor = 0.5,
	) {
		if (baseClip <= 0 || baseClip >= 1) {
			throw new Error("base clip must be in (0, 1)");
		}
		if (quantileThreshold <= 0 || quantileThreshold >= 1) {
			throw new Error("quantile threshold must be in (0, 1)");
		}
		if (shrinkFactor <= 0 || shrinkFactor >= 1) {
			throw new Error("shrink factor must be in (0, 1)");
		}
	}

	clipRange(logRatios: readonly number[]): ClipRange {
		const staleness = logRatios.map(stalenessFromLogRatio);
		const threshold = quantile(staleness, this.quantileThreshold);
		return {
			negative: -this.baseClip,
			positive: this.baseClip,
			threshold,
		};
	}

	clipBounds(logRatio: number, threshold: number): ClipRange {
		const stale = stalenessFromLogRatio(logRatio) >= threshold;
		const scale = stale ? this.shrinkFactor : 1;
		return {
			negative: -this.baseClip * scale,
			positive: this.baseClip * scale,
			threshold,
		};
	}

	objective(
		advantage: number,
		ratio: number,
		logRatio: number,
		threshold: number,
	): number {
		if (!Number.isFinite(advantage) || !Number.isFinite(ratio)) {
			throw new Error("advantage and ratio must be finite");
		}
		const bounds = this.clipBounds(logRatio, threshold);
		const clipped = Math.min(
			bounds.positive,
			Math.max(bounds.negative, ratio),
		);
		return advantage * clipped;
	}
}

export interface SubVerifierResult {
	readonly passed: number;
	readonly total: number;
}

export function t1DenseReward(
	result: SubVerifierResult,
	maxBonus = 0.2,
): number {
	if (result.total <= 0) {
		throw new Error("sub-verifier total must be positive");
	}
	const fraction = Math.min(1, Math.max(0, result.passed / result.total));
	return fraction + maxBonus * fraction;
}
